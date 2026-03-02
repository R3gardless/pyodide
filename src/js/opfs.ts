import { PyodideModule } from "./types";
import { getFsHandles } from "./nativefs";

// FileSystemSyncAccessHandle is only available in Web Worker contexts.
// Declare the interface here to avoid adding WebWorker lib to tsconfig.
interface FileSystemSyncAccessHandle {
  close(): void;
  flush(): void;
  getSize(): number;
  read(buffer: ArrayBufferView, options?: { at?: number }): number;
  truncate(newSize: number): void;
  write(buffer: ArrayBufferView, options?: { at?: number }): number;
}

// Extend FileSystemFileHandle with createSyncAccessHandle (WebWorker-only API)
interface FileSystemFileHandleWithSync extends FileSystemFileHandle {
  createSyncAccessHandle(): Promise<FileSystemSyncAccessHandle>;
}

/**
 * Check if we're running in a dedicated worker where
 * createSyncAccessHandle is available.
 */
function canUseSyncAccessHandle(): boolean {
  return (
    typeof globalThis.constructor !== "undefined" &&
    globalThis.constructor.name === "DedicatedWorkerGlobalScope"
  );
}

/**
 * Initialize the OPFS_WORKER filesystem and register it with Emscripten.
 *
 * When running in a dedicated Web Worker, uses FileSystemSyncAccessHandle
 * for direct synchronous I/O. When running in the main thread, falls back
 * to async File API reads and MEMFS-backed writes with async sync to OPFS.
 *
 * @private
 */
export function initializeOPFS(module: PyodideModule) {
  const FS = module.FS;
  const MEMFS = module.FS.filesystems.MEMFS;
  const PATH = module.PATH;

  const useSync = canUseSyncAccessHandle();

  // Map: Emscripten node.id -> SyncAccessHandle (only used in worker mode)
  const handleMap: Map<number, FileSystemSyncAccessHandle> = new Map();
  // Map: Emscripten node.id -> FileSystemFileHandle
  const fileHandleMap: Map<number, FileSystemFileHandle> = new Map();
  // Map: relative path -> FileSystemDirectoryHandle (for dir ops)
  const dirHandleMap: Map<string, FileSystemDirectoryHandle> = new Map();

  /**
   * Override stream_ops on a node to use SyncAccessHandle for I/O.
   * Only used in worker mode.
   */
  function applyHandleOps(node: any) {
    const origNodeOps = { ...node.node_ops };
    const origStreamOps = { ...node.stream_ops };

    node.node_ops = {
      ...origNodeOps,
      getattr(node: any) {
        const result = origNodeOps.getattr(node);
        const handle = handleMap.get(node.id);
        if (handle) {
          result.size = handle.getSize();
        }
        return result;
      },
      setattr(node: any, attr: any) {
        origNodeOps.setattr(node, attr);
        if (attr.size !== undefined) {
          const handle = handleMap.get(node.id);
          if (handle) {
            handle.truncate(attr.size);
            node.usedBytes = attr.size;
          }
        }
      },
    };

    node.stream_ops = {
      ...origStreamOps,
      open(stream: any) {
        const handle = handleMap.get(stream.node.id);
        if (handle) {
          stream.handle = handle;
        }
      },
      read(
        stream: any,
        buffer: Uint8Array,
        offset: number,
        length: number,
        position: number,
      ): number {
        const handle = stream.handle || handleMap.get(stream.node.id);
        if (!handle) {
          return origStreamOps.read(stream, buffer, offset, length, position);
        }
        if (length === 0) return 0;
        const tempBuf = new Uint8Array(length);
        const bytesRead = handle.read(tempBuf, { at: position });
        buffer.set(tempBuf.subarray(0, bytesRead), offset);
        return bytesRead;
      },
      write(
        stream: any,
        buffer: Uint8Array,
        offset: number,
        length: number,
        position: number,
      ): number {
        const handle = stream.handle || handleMap.get(stream.node.id);
        if (!handle) {
          return origStreamOps.write(stream, buffer, offset, length, position);
        }
        if (length === 0) return 0;
        const data = buffer.subarray(offset, offset + length);
        const bytesWritten = handle.write(data, { at: position });
        const newSize = position + bytesWritten;
        if (newSize > stream.node.usedBytes) {
          stream.node.usedBytes = newSize;
        }
        return bytesWritten;
      },
      llseek(stream: any, offset: number, whence: number): number {
        let position = offset;
        if (whence === 1) {
          position += stream.position;
        } else if (whence === 2) {
          const handle = stream.handle || handleMap.get(stream.node.id);
          if (handle) {
            position += handle.getSize();
          } else if (FS.isFile(stream.node.mode)) {
            position += stream.node.usedBytes;
          }
        }
        if (position < 0) {
          throw new FS.ErrnoError(28); // EINVAL
        }
        return position;
      },
      close(stream: any) {
        const handle = stream.handle || handleMap.get(stream.node.id);
        if (handle) {
          handle.flush();
        }
      },
      fsync(stream: any) {
        const handle = stream.handle || handleMap.get(stream.node.id);
        if (handle) {
          handle.flush();
        }
      },
    };
  }

  /**
   * Walk the OPFS tree, create MEMFS directory/file nodes,
   * and either open SyncAccessHandles (worker) or load file data into MEMFS (main thread).
   */
  async function populateFromOPFS(mount: any) {
    const rootHandle: FileSystemDirectoryHandle =
      mount.opts.fileSystemHandle;
    const handles = await getFsHandles(rootHandle);

    dirHandleMap.set(".", rootHandle);

    const sortedPaths = [...handles.keys()].sort();

    for (const relPath of sortedPaths) {
      if (relPath === ".") continue;

      const handle = handles.get(relPath);
      const absPath = PATH.join2(mount.mountpoint, relPath);

      if (handle.kind === "directory") {
        dirHandleMap.set(relPath, handle);
        try {
          FS.mkdirTree(absPath);
        } catch (_e) {
          // directory may already exist
        }
      } else if (handle.kind === "file") {
        const parentDir = PATH.dirname(absPath);
        try {
          FS.mkdirTree(parentDir);
        } catch (_e) {
          // parent may already exist
        }

        const fileHandle = handle as FileSystemFileHandle;

        if (useSync) {
          // Worker mode: open SyncAccessHandle for direct I/O
          let node;
          try {
            const lookup = FS.lookupPath(absPath, {});
            node = lookup.node;
          } catch (_e) {
            FS.writeFile(absPath, new Uint8Array(0));
            const lookup = FS.lookupPath(absPath, {});
            node = lookup.node;
          }

          const syncHandle: FileSystemSyncAccessHandle =
            await (fileHandle as FileSystemFileHandleWithSync).createSyncAccessHandle();

          handleMap.set(node.id, syncHandle);
          fileHandleMap.set(node.id, fileHandle);
          (node as any).usedBytes = syncHandle.getSize();

          applyHandleOps(node);
        } else {
          // Main thread: read file contents into MEMFS
          const file = await fileHandle.getFile();
          const contents = new Uint8Array(await file.arrayBuffer());
          FS.writeFile(absPath, contents);
          fileHandleMap.set(
            FS.lookupPath(absPath, {}).node.id,
            fileHandle,
          );
        }
      }
    }
  }

  /**
   * Sync local changes to OPFS:
   * - New MEMFS files without handles -> create in OPFS
   * - Modified files -> write MEMFS contents to OPFS
   * - Deleted files -> removeEntry()
   * - In worker mode: flush all open SyncAccessHandles
   */
  async function pushToOPFS(mount: any) {
    const rootHandle: FileSystemDirectoryHandle =
      mount.opts.fileSystemHandle;

    // Collect all current MEMFS paths
    const localPaths = new Set<string>();

    function walkLocal(path: string) {
      let entries;
      try {
        entries = FS.readdir(path);
      } catch (_e) {
        return;
      }
      for (const entry of entries) {
        if (entry === "." || entry === "..") continue;
        const fullPath = PATH.join2(path, entry);
        const stat = FS.stat(fullPath);
        const relPath = PATH.normalize(
          fullPath.replace(mount.mountpoint, "/"),
        ).substring(1);
        localPaths.add(relPath);

        if (FS.isDir(stat.mode)) {
          walkLocal(fullPath);
        }
      }
    }
    walkLocal(mount.mountpoint);

    // Find new files/dirs that don't have OPFS handles yet
    for (const relPath of localPaths) {
      const absPath = PATH.join2(mount.mountpoint, relPath);
      const stat = FS.stat(absPath);

      if (FS.isDir(stat.mode)) {
        if (!dirHandleMap.has(relPath)) {
          const parentRelPath = PATH.dirname(relPath);
          const dirName = PATH.basename(relPath);
          const parentHandle =
            parentRelPath === "."
              ? rootHandle
              : dirHandleMap.get(parentRelPath);
          if (parentHandle) {
            const newDirHandle = await parentHandle.getDirectoryHandle(
              dirName,
              { create: true },
            );
            dirHandleMap.set(relPath, newDirHandle);
          }
        }
      } else if (FS.isFile(stat.mode)) {
        const lookup = FS.lookupPath(absPath, {});
        const node = lookup.node;

        if (useSync && !handleMap.has(node.id)) {
          // Worker mode: new file, create in OPFS + open SyncAccessHandle
          const parentRelPath = PATH.dirname(relPath);
          const fileName = PATH.basename(relPath);
          const parentHandle =
            parentRelPath === "."
              ? rootHandle
              : dirHandleMap.get(parentRelPath);
          if (parentHandle) {
            const fileHandle = await parentHandle.getFileHandle(fileName, {
              create: true,
            });
            const syncHandle = await (
              fileHandle as FileSystemFileHandleWithSync
            ).createSyncAccessHandle();

            handleMap.set(node.id, syncHandle);
            fileHandleMap.set(node.id, fileHandle);

            const contents = MEMFS.getFileDataAsTypedArray(node);
            if (contents.length > 0) {
              syncHandle.write(contents, { at: 0 });
              syncHandle.truncate(contents.length);
            }
            (node as any).usedBytes = contents.length;

            applyHandleOps(node);
          }
        } else if (!useSync) {
          // Main thread: write MEMFS contents to OPFS via async API
          const parentRelPath = PATH.dirname(relPath);
          const fileName = PATH.basename(relPath);
          const parentHandle =
            parentRelPath === "."
              ? rootHandle
              : dirHandleMap.get(parentRelPath);
          if (parentHandle) {
            const fileHandle = await parentHandle.getFileHandle(fileName, {
              create: true,
            });
            const writable = await fileHandle.createWritable();
            const contents = FS.readFile(absPath) as Uint8Array;
            await writable.write(contents as unknown as Blob);
            await writable.close();
            fileHandleMap.set(node.id, fileHandle);
          }
        }
      }
    }

    // Find deleted files: handles exist in OPFS but paths don't exist locally
    const remoteHandles = await getFsHandles(rootHandle);
    for (const [relPath, handle] of remoteHandles) {
      if (relPath === ".") continue;
      if (!localPaths.has(relPath)) {
        if (handle.kind === "file" && useSync) {
          // Close the SyncAccessHandle if in worker mode
          for (const [nodeId, fh] of fileHandleMap) {
            if (
              fh === handle ||
              fh.name === (handle as FileSystemFileHandle).name
            ) {
              const syncHandle = handleMap.get(nodeId);
              if (syncHandle) {
                syncHandle.close();
                handleMap.delete(nodeId);
              }
              fileHandleMap.delete(nodeId);
              break;
            }
          }
        }
        // Remove from OPFS
        const parentRelPath = PATH.dirname(relPath);
        const entryName = PATH.basename(relPath);
        const parentHandle =
          parentRelPath === "."
            ? rootHandle
            : dirHandleMap.get(parentRelPath);
        if (parentHandle) {
          try {
            await parentHandle.removeEntry(entryName, { recursive: true });
          } catch (_e) {
            // entry may already be removed
          }
        }
        dirHandleMap.delete(relPath);
      }
    }

    // Flush all open SyncAccessHandles (worker mode only)
    if (useSync) {
      for (const handle of handleMap.values()) {
        handle.flush();
      }
    }
  }

  const opfsWorker = {
    mount(mount: any) {
      if (!mount.opts.fileSystemHandle) {
        throw new Error("opts.fileSystemHandle is required");
      }
      return MEMFS.mount.apply(null, arguments as any);
    },

    syncfs: async (mount: any, populate: boolean, callback: Function) => {
      try {
        if (populate) {
          await populateFromOPFS(mount);
        } else {
          await pushToOPFS(mount);
        }
        callback(null);
      } catch (e) {
        callback(e);
      }
    },

    unmount(_mount: any) {
      // Close all SyncAccessHandles (worker mode only)
      for (const handle of handleMap.values()) {
        try {
          handle.close();
        } catch (_e) {
          // handle may already be closed
        }
      }
      handleMap.clear();
      fileHandleMap.clear();
      dirHandleMap.clear();
    },
  };

  module.FS.filesystems.OPFS_WORKER = opfsWorker;
}
