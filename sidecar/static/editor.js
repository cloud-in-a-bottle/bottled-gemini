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
// endpoints, "none" for endpoints (like DELETE) that return an empty
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

async function createFile(path) {
    return await apiFetch("/api/files/" + encodePath(path), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({content: ""}),
    });
}

async function deleteFile(path) {
    return await apiFetch("/api/files/" + encodePath(path), {
        method: "DELETE",
    }, "none");
}

// ----------------------------------------------------------------- UI

class Editor {
    constructor(textareaEl, statusEl) {
        this.textarea = textareaEl;
        this.statusEl = statusEl;
        this.dirty = false;
        this.path = null;
        this.textarea.addEventListener("input", () => this.markDirty());
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
        this.textarea.value = src;
        this.textarea.disabled = false;
        this.dirty = false;
        this.setStatus("Loaded " + path);
    }

    clear() {
        this.path = null;
        this.textarea.value = "";
        this.textarea.disabled = true;
        this.dirty = false;
        this.setStatus("No file loaded");
    }

    contents() {
        return this.textarea.value;
    }
}

function renderFileList(listEl, files, onPick, current) {
    while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
    for (const f of files) {
        const a = document.createElement("a");
        a.href = "#";
        a.className = "file-link" + (f === current ? " current" : "");
        a.textContent = f;
        a.addEventListener("click", (e) => {
            e.preventDefault();
            onPick(f);
        });
        listEl.appendChild(a);
    }
}

async function init() {
    const textarea = document.getElementById("editor-host");
    const status = document.getElementById("editor-status");
    const fileList = document.getElementById("file-list");
    const filenameEl = document.getElementById("current-file");
    const editor = new Editor(textarea, status);

    // Disable the textarea until a file is loaded so the user
    // cannot type into a context that has no save target.
    textarea.disabled = true;

    let currentFile = null;

    async function refreshFileList() {
        const files = await listFiles();
        renderFileList(fileList, files, openFile, currentFile);
    }

    async function openFile(path) {
        if (editor.dirty) {
            if (!confirm("Discard unsaved changes?")) return;
        }
        try {
            const content = await loadFile(path);
            currentFile = path;
            editor.load(path, content);
            filenameEl.textContent = path;
            await refreshFileList();
        } catch (err) {
            editor.setStatus("Failed to load: " + err.message, "err");
        }
    }

    document.getElementById("btn-save").addEventListener("click", async () => {
        if (!currentFile) return;
        const body = editor.contents();
        try {
            const result = await saveFile(currentFile, body);
            if (result.feed_status === "error") {
                editor.markSaved("Saved; RSS update failed", "warn");
            } else {
                editor.markSaved();
            }
        } catch (err) {
            editor.setStatus("Save failed: " + err.message, "err");
        }
    });

    document.getElementById("btn-new").addEventListener("click", async () => {
        const name = prompt("New file (e.g. notes.gmi):");
        if (!name) return;
        const path = name.endsWith(".gmi") ? name : name + ".gmi";
        try {
            const result = await createFile(path);
            await refreshFileList();
            await openFile(path);
            if (result.feed_status === "error") {
                editor.setStatus("Created; RSS update failed", "warn");
            }
        } catch (err) {
            editor.setStatus("Create failed: " + err.message, "err");
        }
    });

    document.getElementById("btn-delete").addEventListener("click", async () => {
        if (!currentFile) return;
        if (!confirm(`Delete ${currentFile}? This is permanent.`)) return;
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
        if (editor.dirty) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    // Initial state: list files, open the first one if any.
    try {
        const files = await listFiles();
        renderFileList(fileList, files, openFile, null);
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
