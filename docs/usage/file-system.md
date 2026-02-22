(file-system)=

# Dealing with the file system

Pyodide includes a file system provided by Emscripten. In JavaScript, the
Pyodide file system can be accessed through {js:attr}`pyodide.FS` which re-exports
the [Emscripten File System
API](https://emscripten.org/docs/api_reference/Filesystem-API.html#filesystem-api)

**Example: Reading from the file system**

```pyodide
pyodide.runPython(`
  from pathlib import Path

  Path("/hello.txt").write_text("hello world!")
`);

let file = pyodide.FS.readFile("/hello.txt", { encoding: "utf8" });
console.log(file); // ==> "hello world!"
```

**Example: Writing to the file system**

```pyodide
let data = "hello world!";
pyodide.FS.writeFile("/hello.txt", data, { encoding: "utf8" });
pyodide.runPython(`
  from pathlib import Path

  print(Path("/hello.txt").read_text())
`);
```

## Mounting a file system

The default file system used in Pyodide is [MEMFS](https://emscripten.org/docs/api_reference/Filesystem-API.html#memfs),
which is a virtual in-memory file system. The data stored in MEMFS will be lost when the page is reloaded.

If you wish for files to persist, you can mount other file systems.
Other file systems provided by Emscripten are `IDBFS`, `NODEFS`, `PROXYFS`, `WORKERFS`.
Note that some filesystems can only be used in specific runtime environments.
See [Emscripten File System API](https://emscripten.org/docs/api_reference/Filesystem-API.html#filesystem-api) for more details.
For instance, to store data persistently between page reloads, one could mount
a folder with the
[IDBFS file system](https://emscripten.org/docs/api_reference/Filesystem-API.html#filesystem-api-idbfs)

```pyodide
let mountDir = "/mnt";
pyodide.FS.mkdirTree(mountDir);
pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, mountDir);
```

If you are using Node.js you can access the native file system by mounting `NODEFS`.

```pyodide
let mountDir = "/mnt";
pyodide.FS.mkdirTree(mountDir);
pyodide.FS.mount(pyodide.FS.filesystems.NODEFS, { root: "." }, mountDir);
pyodide.runPython("import os; print(os.listdir('/mnt'))");
// ==> The list of files in the Node working directory
```

(nativefs-api)=

# (Experimental) Using the native file system in the browser

You can access the native file system from the browser using the
[File System Access API](https://developer.mozilla.org/en-US/docs/Web/API/File_System_Access_API).

```{admonition} This is experimental
:class: warning

The File System Access API is only supported in Chromium based browsers: Chrome and Edge (as of 2022/08/18).
```

## Mounting a directory

Pyodide provides an API {js:func}`pyodide.mountNativeFS` which mounts a
{js:class}`FileSystemDirectoryHandle` into the Pyodide Python file system.

```pyodide
const dirHandle = await showDirectoryPicker();
const permissionStatus = await dirHandle.requestPermission({
  mode: "readwrite",
});

if (permissionStatus !== "granted") {
  throw new Error("readwrite access to directory not granted");
}

const nativefs = await pyodide.mountNativeFS("/mount_dir", dirHandle);

pyodide.runPython(`
  import os
  print(os.listdir('/mount_dir'))
`);
```

## Synchronizing changes to native file system

Due to browser limitations, the changes in the mounted file system
is not synchronized by default. In order to persist any operations
to an native file system, you must call

```pyodide
// nativefs is the returned from: await pyodide.mountNativeFS('/mount_dir', dirHandle)
pyodide.runPython(`
  with open('/mount_dir/new_file.txt', 'w') as f:
    f.write("hello");
`);

// new_file.txt does not exist in native file system

await nativefs.syncfs();

// new_file.txt will now exist in native file system
```

or

```js
pyodide.FS.syncfs(false, callback_func);
```

(opfs-api)=

# (Experimental) Using OPFS with synchronous I/O

The {js:func}`pyodide.mountNativeFS` API copies all file contents from the native file
system into MEMFS at mount time, doubling memory usage. For large files or
data-heavy workflows, this can be a problem.

{js:func}`pyodide.mountOPFS` provides an alternative that uses the
[Origin Private File System](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API/Origin_private_file_system)
with `FileSystemSyncAccessHandle` for direct synchronous I/O. File data is
read from and written to OPFS directly -- no copy into MEMFS is needed.

```{admonition} This is experimental
:class: warning

`mountOPFS` requires browser support for the Origin Private File System and
`FileSystemSyncAccessHandle`. It works on Chrome 121+, Edge 121+, Firefox
111+ (Web Worker only), and Safari 17.4+.
```

## Mounting an OPFS directory

```pyodide
// Mount the OPFS root
const opfs = await pyodide.mountOPFS("/mnt/opfs");

// Or mount a subdirectory
const root = await navigator.storage.getDirectory();
const subdir = await root.getDirectoryHandle("mydata", { create: true });
const opfs = await pyodide.mountOPFS("/mnt/opfs", subdir);

pyodide.runPython(`
  import os
  print(os.listdir('/mnt/opfs'))
`);
```

## Reading and writing files

Reads and writes to **existing** files go directly through `FileSystemSyncAccessHandle`
and are immediately persisted to OPFS. No `syncfs()` call is needed for these operations.

```pyodide
pyodide.runPython(`
  # Reading an existing file -- data comes directly from OPFS
  with open('/mnt/opfs/data.csv') as f:
    content = f.read()

  # Writing to an existing file -- data goes directly to OPFS
  with open('/mnt/opfs/data.csv', 'w') as f:
    f.write("updated content")
`);
```

## Creating new files

New files created from Python are initially buffered in MEMFS. Call `syncfs()` to
persist them to OPFS:

```pyodide
pyodide.runPython(`
  with open('/mnt/opfs/new_file.txt', 'w') as f:
    f.write("hello");
`);

// new_file.txt exists in MEMFS but not yet in OPFS
await opfs.syncfs();
// new_file.txt is now persisted in OPFS
```

## Comparison with mountNativeFS

| Feature | `mountNativeFS` | `mountOPFS` |
|---------|----------------|-------------|
| Memory usage | Copies all file data into MEMFS (2x memory) | File data stays in OPFS (no copy) |
| File sources | OPFS or `showDirectoryPicker()` handles | OPFS only (`navigator.storage.getDirectory()`) |
| Read/write existing files | Through MEMFS copy | Direct synchronous I/O via `SyncAccessHandle` |
| New file creation | Through MEMFS, needs `syncfs()` | Through MEMFS, needs `syncfs()` |
| Best for | Small files, broad browser support | Large files, memory-constrained workflows |
