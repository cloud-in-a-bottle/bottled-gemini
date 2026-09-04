// Plain-text source editor for gemtext.
//
// Gemtext (https://geminiprotocol.net/docs/gemtext-specification.gmi) is
// strictly line-oriented and has only six line shapes:
//
//   #, ##, ###  -> headings (level 1-3)
//   =>          -> link
//   *           -> list item
//   >           -> blockquote
//   ```         -> toggle preformatted block
//   anything    -> paragraph
//
// We deliberately do NOT try to be a WYSIWYG editor. Gemtext is small
// enough that a plain textarea -- with the file list, save/new/delete
// controls, and live save status -- is a better fit than a contenteditable
// host with shape buttons and a paste normaliser. The user types
// gemtext, hits save, and the capsule serves it.

// ----------------------------------------------------------------- API client

// Encode a path while preserving "/" separators. encodeURIComponent
// alone would escape slashes, and Starlette's `{rel:path}` converter
// receives the percent-encoded form -- which fails our path-validation
// regex and 400s every subdirectory file. Encode each segment
// individually instead.
function encodePath(p) {
    return p.split("/").map(encodeURIComponent).join("/");
}

// All API helpers go through this single fetch wrapper so credentials
// handling, error formatting, and JSON parsing live in one place.
// `parse` controls what we read off the response: "json" for normal
// endpoints, "blob" for downloads, and "none" for endpoints that return an empty
// body. We always read the response body on a non-OK status so the
// thrown Error includes the server's text/JSON error detail.
async function apiFetch(url, opts = {}, parse = "json") {
    const r = await fetch(url, {credentials: "same-origin", ...opts});
    if (!r.ok) {
        // Best-effort body read so the thrown error includes the
        // server's message. If the read itself fails (network blip,
        // body already consumed) note that, rather than producing
        // an empty-detail error that's harder to debug.
        let detail;
        try {
            detail = await r.text();
        } catch (readErr) {
            detail = `(could not read response body: ${readErr.message})`;
        }
        throw new Error(`${r.status} ${r.statusText}: ${detail}`);
    }
    if (parse === "none") return r;
    if (parse === "blob") return await r.blob();
    return await r.json();
}

async function listFiles() {
    const data = await apiFetch("/api/files");
    return data.files;
}

async function loadFile(path) {
    const data = await apiFetch("/api/files/" + encodePath(path));
    return data.content;
}

async function saveFile(path, content) {
    return await apiFetch("/api/files/" + encodePath(path), {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({content}),
    });
}

async function createFile(path, content = "") {
    return await apiFetch("/api/files/" + encodePath(path), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({content}),
    });
}

async function deleteFile(path) {
    return await apiFetch("/api/files/" + encodePath(path), {
        method: "DELETE",
    }, "none");
}

async function downloadArchive(paths) {
    return await apiFetch("/api/download", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({paths}),
    }, "blob");
}

async function downloadSingle(path) {
    return await apiFetch("/api/download/" + encodePath(path), {}, "blob");
}

function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ----------------------------------------------------------------- UI

class Editor {
    constructor(textareaEl, statusEl) {
        this.textarea = textareaEl;
        this.statusEl = statusEl;
        this.dirty = false;
        this.path = null;
        this.revision = 0;
        this.textarea.addEventListener("input", () => {
            this.revision += 1;
            this.markDirty();
        });
    }

    setStatus(text, klass = "") {
        this.statusEl.textContent = text;
        this.statusEl.className = "editor-status " + klass;
    }

    markDirty() {
        if (!this.dirty) {
            this.dirty = true;
            this.setStatus("Unsaved changes", "warn");
        }
    }

    markSaved(text = "Saved", klass = "ok") {
        this.dirty = false;
        this.setStatus(text, klass);
    }

    load(path, src) {
        this.path = path;
        this.revision += 1;
        this.textarea.value = src;
        this.textarea.disabled = false;
        this.dirty = false;
        this.setStatus("Loaded " + path);
    }

    clear() {
        this.path = null;
        this.revision += 1;
        this.textarea.value = "";
        this.textarea.disabled = true;
        this.dirty = false;
        this.setStatus("No file loaded");
    }

    contents() {
        return this.textarea.value;
    }
}

function renderFileList(listEl, files, onPick, current, selected, onSelection) {
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
    for (const f of files) {
        const row = document.createElement("div");
        row.className = "file-row" + (f === current ? " current" : "");

        const selectLabel = document.createElement("label");
        selectLabel.className = "file-select-label";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "file-select";
        checkbox.checked = selected.has(f);
        checkbox.setAttribute("aria-label", "Select " + f);
        checkbox.addEventListener("change", () => onSelection(f, checkbox.checked));
        selectLabel.appendChild(checkbox);

        const a = document.createElement("a");
        a.href = "#";
        a.className = "file-link" + (f === current ? " current" : "");
        a.textContent = f;
        a.addEventListener("click", (e) => {
            e.preventDefault();
            onPick(f);
        });
        row.append(selectLabel, a);
        listEl.appendChild(row);
    }
}

async function init() {
    const textarea = document.getElementById("editor-host");
    const status = document.getElementById("editor-status");
    const fileList = document.getElementById("file-list");
    const filenameEl = document.getElementById("current-file");
    const fileUpload = document.getElementById("file-upload");
    const selectAll = document.getElementById("select-all");
    const downloadButton = document.getElementById("btn-download");
    const saveButton = document.getElementById("btn-save");
    const transferStatus = document.getElementById("transfer-status");
    const editor = new Editor(textarea, status);

    // Disable the textarea until a file is loaded so the user
    // cannot type into a context that has no save target.
    textarea.disabled = true;

    let currentFile = null;
    let filesInList = [];
    let writeInProgress = false;
    let navigationInProgress = false;
    let downloadInProgress = false;
    const selectedFiles = new Set();

    function setTransferStatus(text, klass = "") {
        transferStatus.textContent = text;
        transferStatus.className = "transfer-status " + klass;
    }

    function updateSelectionControls() {
        const selectedCount = filesInList.filter((path) => selectedFiles.has(path)).length;
        selectAll.disabled = filesInList.length === 0;
        selectAll.checked = filesInList.length > 0 && selectedCount === filesInList.length;
        selectAll.indeterminate = selectedCount > 0 && selectedCount < filesInList.length;
        downloadButton.disabled = selectedCount === 0 || downloadInProgress;
        downloadButton.textContent = selectedCount > 0 ? `Download (${selectedCount})` : "Download";
    }

    function renderCurrentFileList() {
        renderFileList(fileList, filesInList, openFile, currentFile, selectedFiles, (path, selected) => {
            if (selected) {
                selectedFiles.add(path);
            } else {
                selectedFiles.delete(path);
            }
            updateSelectionControls();
        });
        updateSelectionControls();
    }

    async function refreshFileList() {
        filesInList = await listFiles();
        for (const selected of selectedFiles) {
            if (!filesInList.includes(selected)) selectedFiles.delete(selected);
        }
        renderCurrentFileList();
    }

    async function openFile(path) {
        if (writeInProgress || navigationInProgress) {
            setTransferStatus("Wait for the current file operation to finish", "warn");
            return;
        }
        if (editor.dirty) {
            if (!confirm("Discard unsaved changes?")) return;
        }
        navigationInProgress = true;
        const textareaWasDisabled = textarea.disabled;
        textarea.disabled = true;
        let loaded = false;
        try {
            const content = await loadFile(path);
            currentFile = path;
            editor.load(path, content);
            loaded = true;
            filenameEl.textContent = path;
            await refreshFileList();
        } catch (err) {
            editor.setStatus("Failed to load: " + err.message, "err");
        } finally {
            if (!loaded) textarea.disabled = textareaWasDisabled;
            navigationInProgress = false;
        }
    }

    saveButton.addEventListener("click", async () => {
        if (!currentFile) return;
        if (writeInProgress || navigationInProgress) {
            setTransferStatus("Wait for the current file operation to finish", "warn");
            return;
        }
        const body = editor.contents();
        const revision = editor.revision;
        writeInProgress = true;
        saveButton.disabled = true;
        try {
            const result = await saveFile(currentFile, body);
            if (editor.revision !== revision) {
                const message = result.feed_status === "error"
                    ? "Saved earlier changes; newer local edits kept; RSS update failed"
                    : "Saved earlier changes; newer local edits kept";
                setTransferStatus(message, "warn");
            } else if (result.feed_status === "error") {
                editor.markSaved("Saved; RSS update failed", "warn");
            } else {
                editor.markSaved();
            }
        } catch (err) {
            editor.setStatus("Save failed: " + err.message, "err");
        } finally {
            writeInProgress = false;
            saveButton.disabled = false;
        }
    });

    document.getElementById("btn-new").addEventListener("click", async () => {
        if (writeInProgress || navigationInProgress) {
            setTransferStatus("Wait for the current file operation to finish", "warn");
            return;
        }
        const name = prompt("New file (e.g. notes.gmi):");
        if (!name) return;
        const path = name.endsWith(".gmi") ? name : name + ".gmi";
        let result;
        writeInProgress = true;
        saveButton.disabled = true;
        try {
            result = await createFile(path);
            await refreshFileList();
        } catch (err) {
            editor.setStatus("Create failed: " + err.message, "err");
        } finally {
            writeInProgress = false;
            saveButton.disabled = false;
        }
        if (result) {
            await openFile(path);
            if (result.feed_status === "error") {
                editor.setStatus("Created; RSS update failed", "warn");
            }
        }
    });

    fileUpload.addEventListener("change", async () => {
        if (writeInProgress || navigationInProgress) {
            setTransferStatus("Wait for the current file operation to finish, then upload again", "warn");
            fileUpload.value = "";
            return;
        }
        const uploads = [...fileUpload.files];
        const uploaded = [];
        const errors = [];
        let feedWarnings = 0;
        let concurrentEdits = 0;
        writeInProgress = true;
        fileUpload.disabled = true;
        saveButton.disabled = true;
        setTransferStatus("Uploading...");
        try {
            for (const file of uploads) {
                try {
                    const editorRevision = editor.revision;
                    if (!file.name.endsWith(".gmi")) {
                        throw new Error("only .gmi files can be uploaded");
                    }
                    if (file.size > 1024 * 1024) {
                        throw new Error("file exceeds 1 MiB");
                    }
                    let content;
                    try {
                        content = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(
                            await file.arrayBuffer(),
                        );
                    } catch {
                        throw new Error("file is not valid UTF-8");
                    }
                    if (new Blob([content]).size > 1024 * 1024) {
                        throw new Error("decoded file exceeds 1 MiB");
                    }
                    let result;
                    if (filesInList.includes(file.name)) {
                        if (!confirm(`${file.name} already exists. Replace it?`)) continue;
                        result = await saveFile(file.name, content);
                    } else {
                        try {
                            result = await createFile(file.name, content);
                        } catch (err) {
                            if (!err.message.startsWith("409 ") || !confirm(`${file.name} already exists. Replace it?`)) {
                                throw err;
                            }
                            result = await saveFile(file.name, content);
                        }
                    }
                    uploaded.push(file.name);
                    selectedFiles.add(file.name);
                    if (result.feed_status === "error") feedWarnings += 1;
                    if (file.name === currentFile) {
                        if (editor.revision === editorRevision) {
                            editor.load(currentFile, content);
                        } else {
                            concurrentEdits += 1;
                        }
                    }
                } catch (err) {
                    errors.push(`${file.name}: ${err.message}`);
                }
            }
            try {
                await refreshFileList();
            } catch (err) {
                errors.push(`file list: ${err.message}`);
            }
            const messages = [];
            if (uploaded.length > 0) messages.push(`${uploaded.length} uploaded`);
            if (errors.length > 0) messages.push(`${errors.length} failed`);
            if (feedWarnings > 0) messages.push("RSS update failed");
            if (concurrentEdits > 0) messages.push("local edits kept");
            if (messages.length === 0) {
                setTransferStatus("");
            } else {
                const klass = errors.length > 0 || feedWarnings > 0 || concurrentEdits > 0 ? "warn" : "ok";
                setTransferStatus(messages.join("; "), klass);
            }
        } finally {
            writeInProgress = false;
            fileUpload.disabled = false;
            saveButton.disabled = false;
            fileUpload.value = "";
        }
    });

    selectAll.addEventListener("change", () => {
        selectedFiles.clear();
        if (selectAll.checked) {
            for (const path of filesInList) selectedFiles.add(path);
        }
        renderCurrentFileList();
    });

    downloadButton.addEventListener("click", async () => {
        const paths = filesInList.filter((path) => selectedFiles.has(path));
        if (paths.length === 0 || downloadInProgress) return;
        downloadInProgress = true;
        updateSelectionControls();
        setTransferStatus("Preparing download...");
        try {
            if (paths.length === 1) {
                triggerDownload(
                    await downloadSingle(paths[0]),
                    paths[0].split("/").pop(),
                );
            } else {
                triggerDownload(await downloadArchive(paths), "gemini-pages.zip");
            }
            setTransferStatus(`${paths.length} downloaded`, "ok");
        } catch (err) {
            setTransferStatus("Download failed: " + err.message, "err");
        } finally {
            downloadInProgress = false;
            updateSelectionControls();
        }
    });

    document.getElementById("btn-delete").addEventListener("click", async () => {
        if (writeInProgress || navigationInProgress) {
            setTransferStatus("Wait for the current file operation to finish", "warn");
            return;
        }
        if (!currentFile) return;
        if (!confirm(`Delete ${currentFile}? This is permanent.`)) return;
        writeInProgress = true;
        saveButton.disabled = true;
        try {
            const result = await deleteFile(currentFile);
            currentFile = null;
            filenameEl.textContent = "(no file)";
            editor.clear();
            await refreshFileList();
            if (result.headers.get("X-RSS-Feed-Status") === "error") {
                editor.setStatus("Deleted; RSS update failed", "warn");
            }
        } catch (err) {
            editor.setStatus("Delete failed: " + err.message, "err");
        } finally {
            writeInProgress = false;
            saveButton.disabled = false;
        }
    });

    // Ctrl/Cmd+S triggers save, the way every editor on the planet
    // does it. Stop the browser from opening "Save Page As".
    document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            document.getElementById("btn-save").click();
        }
    });

    window.addEventListener("beforeunload", (e) => {
        if (editor.dirty || writeInProgress) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    // Initial state: list files, open the first one if any.
    try {
        const files = await listFiles();
        filesInList = files;
        renderCurrentFileList();
        if (files.length > 0) {
            await openFile(files[0]);
        } else {
            editor.setStatus("No files yet. Click 'New file' to start.");
        }
    } catch (err) {
        editor.setStatus("Failed to list files: " + err.message, "err");
    }
}

document.addEventListener("DOMContentLoaded", init);
