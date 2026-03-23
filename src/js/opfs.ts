import { PyodideModule } from "./types";

// https://developer.mozilla.org/en-US/docs/Web/API/FileSystemSyncAccessHandle
interface FileSystemSyncAccessHandle {
    close(): void;
    flush(): void;
    getSize(): number;
    read(buffer: ArrayBufferView, options?: { at?: number }): number;
    write(buffer: ArrayBufferView, options?: { at?: number }): number;
    truncate(newSize: number): void;
}

export function initializeOPFS(module: PyodideModule) {

    const opfsWorkerFS = {
        DIR_MODE : 16384 | 511,
        FILE_MODE: 32768 | 511,

        mount(mount: any) {
            if(!mount.opts.opfsHandle) {
                throw new Error("opts.opfsHandle is required");
            }
            return MEMFS.mount.apply(null, arguments as any);
        }
    };

    module.FS.filesystems.OPFS_WORKER_FS = opfsWorkerFS;
};

