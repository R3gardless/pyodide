"""tests of using the emscripten filesystem API with pyodide

for a basic nodejs-based test, see src/js/test/filesystem.test.js
"""

import pytest
from pytest_pyodide import run_in_pyodide

from conftest import only_chrome, only_node


@pytest.mark.skip_refcount_check
@pytest.mark.skip_pyproxy_check
def test_idbfs_persist_code(selenium_standalone):
    """can we persist files created by user python code?"""
    selenium = selenium_standalone
    if selenium.browser == "node":
        fstype = "NODEFS"
    else:
        fstype = "IDBFS"

    mount_dir = "/mount_test"
    # create mount
    selenium.run_js(
        f"""
        let mountDir = '{mount_dir}';
        pyodide.FS.mkdir(mountDir);
        pyodide.FS.mount(pyodide.FS.filesystems.{fstype}, {{root : "."}}, mountDir);
        """
    )

    @run_in_pyodide
    def create_test_file(selenium_module, mount_dir):
        import sys
        from importlib import invalidate_caches
        from pathlib import Path

        p = Path(f"{mount_dir}/test_idbfs/__init__.py")
        p.parent.mkdir(exist_ok=True, parents=True)
        p.write_text("def test(): return 7")
        invalidate_caches()
        sys.path.append(mount_dir)
        from test_idbfs import test

        assert test() == 7

    create_test_file(selenium, mount_dir)
    # sync TO idbfs
    selenium.run_js(
        """
        const error = await new Promise(
            (resolve, reject) => pyodide.FS.syncfs(false, resolve)
        );
        assert(() => error == null);
        """
    )
    # refresh page and re-fixture
    selenium.refresh()
    selenium.run_js(
        """
        self.pyodide = await loadPyodide({ fullStdLib: false });
        """
    )
    # idbfs isn't magically loaded
    selenium.run_js(
        f"""
        pyodide.runPython(`
            from importlib import invalidate_caches
            import sys
            invalidate_caches()
            err_type = None
            try:
                sys.path.append('{mount_dir}')
                from test_idbfs import test
            except Exception as err:
                err_type = type(err)
            assert err_type is ModuleNotFoundError, err_type
        `);
        """
    )
    # re-mount
    selenium.run_js(
        f"""
        pyodide.FS.mkdir('{mount_dir}');
        pyodide.FS.mount(pyodide.FS.filesystems.{fstype}, {{root : "."}}, "{mount_dir}");
        """
    )
    # sync FROM idbfs
    selenium.run_js(
        """
        const error = await new Promise(
            (resolve, reject) => pyodide.FS.syncfs(true, resolve)
        );
        assert(() => error == null);
        """
    )
    # import file persisted above
    selenium.run_js(
        f"""
        pyodide.runPython(`
            from importlib import invalidate_caches
            invalidate_caches()
            import sys
            sys.path.append('{mount_dir}')
            from test_idbfs import test
            assert test() == 7
        `);
        """
    )
    # remove file
    selenium.run_js(f"""pyodide.FS.unlink("{mount_dir}/test_idbfs/__init__.py")""")


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_nativefs_dir(request, selenium_standalone):
    # Note: Using *real* native file system requires
    # user interaction so it is not available in headless mode.
    # So in this test we use OPFS (Origin Private File System)
    # which is part of File System Access API but uses indexDB as a backend.

    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandleMount = await root.getDirectoryHandle('testdir', { create: true });
        testFileHandle = await dirHandleMount.getFileHandle('test_read', { create: true });
        writable = await testFileHandle.createWritable();
        await writable.write("hello_read");
        await writable.close();
        fs = await pyodide.mountNativeFS("/mnt/nativefs", dirHandleMount);
        """
    )

    # Read

    selenium.run(
        """
        import os
        import pathlib
        assert len(os.listdir("/mnt/nativefs")) == 1, str(os.listdir("/mnt/nativefs"))
        assert os.listdir("/mnt/nativefs") == ["test_read"], str(os.listdir("/mnt/nativefs"))

        pathlib.Path("/mnt/nativefs/test_read").read_text() == "hello_read"
        """
    )

    # Write / Delete / Rename

    selenium.run(
        """
        import os
        import pathlib
        pathlib.Path("/mnt/nativefs/test_write").write_text("hello_write")
        pathlib.Path("/mnt/nativefs/test_write").read_text() == "hello_write"
        pathlib.Path("/mnt/nativefs/test_delete").write_text("This file will be deleted")
        pathlib.Path("/mnt/nativefs/test_rename").write_text("This file will be renamed")
        """
    )

    entries = selenium.run_js(
        """
        await fs.syncfs();
        entries = {};
        for await (const [key, value] of dirHandleMount.entries()) {
            entries[key] = value;
        }
        return entries;
        """
    )

    assert "test_read" in entries
    assert "test_write" in entries
    assert "test_delete" in entries
    assert "test_rename" in entries

    selenium.run(
        """
        import os
        os.remove("/mnt/nativefs/test_delete")
        os.rename("/mnt/nativefs/test_rename", "/mnt/nativefs/test_rename_renamed")
        """
    )

    entries = selenium.run_js(
        """
        await fs.syncfs();
        entries = {};
        for await (const [key, value] of dirHandleMount.entries()) {
            entries[key] = value;
        }
        return entries;
        """
    )

    assert "test_delete" not in entries
    assert "test_rename" not in entries
    assert "test_rename_renamed" in entries

    # unmount

    files = selenium.run(
        """
        import os
        os.listdir("/mnt/nativefs")
        """
    )

    assert "test_read" in entries
    assert "test_write" in entries
    assert "test_rename_renamed" in entries

    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/nativefs");
        """
    )

    files = selenium.run(
        """
        import os
        os.listdir("/mnt/nativefs")
        """
    )

    assert not len(files)

    # Mount again

    selenium.run_js(
        """
        fs2 = await pyodide.mountNativeFS("/mnt/nativefs", dirHandleMount);
        """
    )

    # Read again

    selenium.run(
        """
        import os
        import pathlib
        assert len(os.listdir("/mnt/nativefs")) == 3, str(os.listdir("/mnt/nativefs"))
        pathlib.Path("/mnt/nativefs/test_read").read_text() == "hello_read"
        """
    )

    selenium.run_js(
        """
        await fs2.syncfs();
        pyodide.FS.unmount("/mnt/nativefs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_read(request, selenium_standalone):
    """Create file in OPFS via JS, mount with mountOPFS, read from Python"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_read', { create: true });
        testFileHandle = await dirHandle.getFileHandle('hello.txt', { create: true });
        writable = await testFileHandle.createWritable();
        await writable.write("hello from opfs");
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import os
        import pathlib
        assert os.listdir("/mnt/opfs") == ["hello.txt"], str(os.listdir("/mnt/opfs"))
        assert pathlib.Path("/mnt/opfs/hello.txt").read_text() == "hello from opfs"
        """
    )

    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_write_existing(request, selenium_standalone):
    """Mount, write to existing file from Python, verify content persists immediately"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_write', { create: true });
        testFileHandle = await dirHandle.getFileHandle('data.txt', { create: true });
        writable = await testFileHandle.createWritable();
        await writable.write("original content");
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    # Write to the existing file (goes through SyncAccessHandle directly)
    selenium.run(
        """
        import pathlib
        pathlib.Path("/mnt/opfs/data.txt").write_text("updated content")
        assert pathlib.Path("/mnt/opfs/data.txt").read_text() == "updated content"
        """
    )

    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_new_file(request, selenium_standalone):
    """Create new file from Python, call syncfs(), verify in OPFS via JS"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_new', { create: true });
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import pathlib
        pathlib.Path("/mnt/opfs/new_file.txt").write_text("brand new file")
        """
    )

    entries = selenium.run_js(
        """
        await fs.syncfs();
        entries = {};
        for await (const [key, value] of dirHandle.entries()) {
            entries[key] = value.kind;
        }
        return entries;
        """
    )

    assert "new_file.txt" in entries

    selenium.run_js(
        """
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_directory_ops(request, selenium_standalone):
    """List dirs, create subdirs, verify structure"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_dirs', { create: true });
        subDir = await dirHandle.getDirectoryHandle('subdir', { create: true });
        fileHandle = await subDir.getFileHandle('nested.txt', { create: true });
        writable = await fileHandle.createWritable();
        await writable.write("nested content");
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import os
        import pathlib
        assert "subdir" in os.listdir("/mnt/opfs")
        assert "nested.txt" in os.listdir("/mnt/opfs/subdir")
        assert pathlib.Path("/mnt/opfs/subdir/nested.txt").read_text() == "nested content"
        """
    )

    # Create a new subdirectory from Python and syncfs
    selenium.run(
        """
        import os
        os.makedirs("/mnt/opfs/newsubdir", exist_ok=True)
        pathlib.Path("/mnt/opfs/newsubdir/test.txt").write_text("test in subdir")
        """
    )

    entries = selenium.run_js(
        """
        await fs.syncfs();
        entries = {};
        for await (const [key, value] of dirHandle.entries()) {
            entries[key] = value.kind;
        }
        return entries;
        """
    )

    assert "subdir" in entries
    assert "newsubdir" in entries

    selenium.run_js(
        """
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_large_file(request, selenium_standalone):
    """Write/read a large file (~1MB), verify correct content"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_large', { create: true });
        testFileHandle = await dirHandle.getFileHandle('large.bin', { create: true });

        // Write 1MB of data
        const data = new Uint8Array(1024 * 1024);
        for (let i = 0; i < data.length; i++) {
            data[i] = i & 0xff;
        }
        writable = await testFileHandle.createWritable();
        await writable.write(data);
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import os
        import pathlib

        data = pathlib.Path("/mnt/opfs/large.bin").read_bytes()
        assert len(data) == 1024 * 1024
        # Verify pattern
        for i in range(0, len(data), 4096):
            assert data[i] == i & 0xff, f"Mismatch at byte {i}: {data[i]} != {i & 0xff}"
        """
    )

    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_unmount_remount(request, selenium_standalone):
    """Write, unmount, remount, verify data persists"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_remount', { create: true });
        testFileHandle = await dirHandle.getFileHandle('persist.txt', { create: true });
        writable = await testFileHandle.createWritable();
        await writable.write("persistent data");
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import pathlib
        assert pathlib.Path("/mnt/opfs/persist.txt").read_text() == "persistent data"
        pathlib.Path("/mnt/opfs/persist.txt").write_text("modified data")
        """
    )

    # Unmount
    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )

    # Verify empty after unmount
    files = selenium.run(
        """
        import os
        os.listdir("/mnt/opfs")
        """
    )
    assert not len(files)

    # Remount
    selenium.run_js(
        """
        fs2 = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import pathlib
        assert pathlib.Path("/mnt/opfs/persist.txt").read_text() == "modified data"
        """
    )

    selenium.run_js(
        """
        await fs2.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_errors(request, selenium):
    """Invalid handle, already mounted, non-empty dir"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium.run_js(
        """
        const root = await navigator.storage.getDirectory();
        const handle = await root.getDirectoryHandle("opfs_err_dir", { create: true });

        await pyodide.mountOPFS("/mnt1/opfs", handle);
        await assertThrowsAsync(
          async () => await pyodide.mountOPFS("/mnt1/opfs", handle),
          "Error",
          "path '/mnt1/opfs' is already a file system mount point",
        );

        pyodide.FS.mkdirTree("/mnt2");
        pyodide.FS.writeFile("/mnt2/some_file", "contents");
        await assertThrowsAsync(
          async () => await pyodide.mountOPFS("/mnt2/some_file", handle),
          "Error",
          "path '/mnt2/some_file' points to a file not a directory",
        );
        // Check we didn't overwrite the file.
        assert(
          () =>
            pyodide.FS.readFile("/mnt2/some_file", { encoding: "utf8" }) === "contents",
        );

        pyodide.FS.mkdirTree("/mnt3/opfs");
        pyodide.FS.writeFile("/mnt3/opfs/a.txt", "contents");
        await assertThrowsAsync(
          async () => await pyodide.mountOPFS("/mnt3/opfs", handle),
          "Error",
          "directory '/mnt3/opfs' is not empty",
        );
        """
    )


@pytest.mark.requires_dynamic_linking
@only_chrome
def test_opfs_worker_truncate(request, selenium_standalone):
    """Truncate file, verify size changes via both Python and JS"""
    if request.config.option.runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    selenium = selenium_standalone

    selenium.run_js(
        """
        root = await navigator.storage.getDirectory();
        dirHandle = await root.getDirectoryHandle('test_opfs_trunc', { create: true });
        testFileHandle = await dirHandle.getFileHandle('trunc.txt', { create: true });
        writable = await testFileHandle.createWritable();
        await writable.write("hello world, this is a long string");
        await writable.close();
        fs = await pyodide.mountOPFS("/mnt/opfs", dirHandle);
        """
    )

    selenium.run(
        """
        import os
        import pathlib
        p = pathlib.Path("/mnt/opfs/trunc.txt")
        original = p.read_text()
        assert len(original) == 34

        # Truncate via Python
        with open("/mnt/opfs/trunc.txt", "r+") as f:
            f.truncate(5)

        assert os.path.getsize("/mnt/opfs/trunc.txt") == 5
        assert p.read_text() == "hello"
        """
    )

    selenium.run_js(
        """
        await fs.syncfs();
        pyodide.FS.unmount("/mnt/opfs");
        """
    )


@only_chrome
def test_nativefs_errors(selenium):
    selenium.run_js(
        """
        const root = await navigator.storage.getDirectory();
        const handle = await root.getDirectoryHandle("dir", { create: true });

        await pyodide.mountNativeFS("/mnt1/nativefs", handle);
        await assertThrowsAsync(
          async () => await pyodide.mountNativeFS("/mnt1/nativefs", handle),
          "Error",
          "path '/mnt1/nativefs' is already a file system mount point",
        );

        pyodide.FS.mkdirTree("/mnt2");
        pyodide.FS.writeFile("/mnt2/some_file", "contents");
        await assertThrowsAsync(
          async () => await pyodide.mountNativeFS("/mnt2/some_file", handle),
          "Error",
          "path '/mnt2/some_file' points to a file not a directory",
        );
        // Check we didn't overwrite the file.
        assert(
          () =>
            pyodide.FS.readFile("/mnt2/some_file", { encoding: "utf8" }) === "contents",
        );

        pyodide.FS.mkdirTree("/mnt3/nativefs");
        pyodide.FS.writeFile("/mnt3/nativefs/a.txt", "contents");
        await assertThrowsAsync(
          async () => await pyodide.mountNativeFS("/mnt3/nativefs", handle),
          "Error",
          "directory '/mnt3/nativefs' is not empty",
        );
        // Check directory wasn't changed
        const { node } = pyodide.FS.lookupPath("/mnt3/nativefs/");
        assert(() => Object.entries(node.contents).length === 1);
        assert(
          () =>
            pyodide.FS.readFile("/mnt3/nativefs/a.txt", { encoding: "utf8" }) ===
            "contents",
        );

        const [r1, r2] = await Promise.allSettled([
          pyodide.mountNativeFS("/mnt4/nativefs", handle),
          pyodide.mountNativeFS("/mnt4/nativefs", handle),
        ]);
        assert(() => r1.status === "fulfilled");
        assert(() => r2.status === "rejected");
        assert(
          () =>
            r2.reason.message === "path '/mnt4/nativefs' is already a file system mount point",
        );
        """
    )


@only_node
def test_mount_nodefs(selenium):
    selenium.run_js(
        """
        pyodide.mountNodeFS("/mnt1/nodefs", ".");
        assertThrows(
          () => pyodide.mountNodeFS("/mnt1/nodefs", "."),
          "Error",
          "path '/mnt1/nodefs' is already a file system mount point"
        );

        assertThrows(
          () =>
            pyodide.mountNodeFS(
              "/mnt2/nodefs",
              "/thispath/does-not/exist/ihope"
            ),
          "Error",
          "hostPath '/thispath/does-not/exist/ihope' does not exist"
        );

        const os = require("os");
        const fs = require("fs");
        const path = require("path");
        const crypto = require("crypto");
        const tmpdir = path.join(os.tmpdir(), crypto.randomUUID());
        fs.mkdirSync(tmpdir);
        const apath = path.join(tmpdir, "a");
        fs.writeFileSync(apath, "xyz");
        pyodide.mountNodeFS("/mnt3/nodefs", tmpdir);
        assert(
          () =>
            pyodide.FS.readFile("/mnt3/nodefs/a", { encoding: "utf8" }) ===
            "xyz"
        );

        assertThrows(
          () => pyodide.mountNodeFS("/mnt4/nodefs", apath),
          "Error",
          `hostPath '${apath}' is not a directory`
        );
        """
    )


@pytest.fixture
def browser(selenium):
    return selenium.browser


@pytest.fixture
def runner(request):
    return request.config.option.runner


@run_in_pyodide
def test_fs_dup(selenium, browser):
    from os import close, dup
    from pathlib import Path

    from pyodide.code import run_js

    if browser == "node":
        fstype = "NODEFS"
    else:
        fstype = "IDBFS"

    mount_dir = Path("/mount_test")
    mount_dir.mkdir(exist_ok=True)
    run_js(
        """
        (fstype, mountDir) =>
            pyodide.FS.mount(pyodide.FS.filesystems[fstype], {root : "."}, mountDir);
        """
    )(fstype, str(mount_dir))

    file = open("/mount_test/a.txt", "w")
    fd2 = dup(file.fileno())
    close(fd2)
    file.write("abcd")
    file.close()


@pytest.mark.requires_dynamic_linking
@only_chrome
@run_in_pyodide
async def test_nativefs_dup(selenium, runner):
    from os import close, dup

    import pytest

    from pyodide.code import run_js

    # Note: Using *real* native file system requires
    # user interaction so it is not available in headless mode.
    # So in this test we use OPFS (Origin Private File System)
    # which is part of File System Access API but uses indexDB as a backend.

    if runner == "playwright":
        pytest.xfail("Playwright doesn't support file system access APIs")

    await run_js(
        """
        async () => {
            root = await navigator.storage.getDirectory();
            testFileHandle = await root.getFileHandle('test_read', { create: true });
            writable = await testFileHandle.createWritable();
            await writable.write("hello_read");
            await writable.close();
            await pyodide.mountNativeFS("/mnt/nativefs", root);
        }
        """
    )()
    file = open("/mnt/nativefs/test_read")
    fd2 = dup(file.fileno())
    close(fd2)
    assert file.read() == "hello_read"
    file.close()


def test_trackingDelegate(selenium_standalone):
    selenium = selenium_standalone

    selenium.run_js(
        """
        assert (() => typeof pyodide.FS.trackingDelegate !== "undefined")

        if (typeof window !== "undefined") {
            global = window
        } else {
            global = globalThis
        }

        global.trackingLog = ""
        pyodide.FS.trackingDelegate["onCloseFile"] = (path) => { global.trackingLog = `CALLED ${path}` }
        """
    )

    selenium.run(
        """
        f = open("/hello", "w")
        f.write("helloworld")
        f.close()

        import js

        assert "CALLED /hello" in js.trackingLog
        """
    )

    # logs = selenium.logs
    # assert "CALLED /hello" in logs
