## Interactive Mode (Long-Running FreeCAD)

For iterative work — exploring geometry, debugging dimensions, quick experiments — use a
persistent FreeCAD instance with an XML-RPC server. State persists across calls: documents,
objects, and variables survive between commands.

Uses the [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) addon which
starts an XML-RPC server on port 9875 inside FreeCAD. Claude drives it via stdlib
`xmlrpc.client` (zero dependencies).

### When to use interactive vs standalone scripts

| Use case                            | Approach                                              |
| ----------------------------------- | ----------------------------------------------------- |
| Batch/reproducible builds, CI tests | Standalone scripts (`freecadcmd script.py`)           |
| Iterative geometry exploration      | Interactive mode (this section)                       |
| Debugging dimensions/edges          | Interactive mode — tweak and screenshot in a loop     |
| Quick experiments                   | Interactive mode — faster than restarting per command |

### Setup (once per session)

```bash
# 1. Clone the addon
git clone https://github.com/neka-nat/freecad-mcp /tmp/freecad-mcp

# 2. Set up hermetic FreeCAD home (avoids version-migration dialogs)
export FREECAD_USER_HOME=$(mktemp -d)
mkdir -p "$FREECAD_USER_HOME/.local/share/FreeCAD/1.0"

# 3. Enable auto-start of RPC server
# getUserAppDataDir() returns $FREECAD_USER_HOME/ directly
cat > "$FREECAD_USER_HOME/freecad_mcp_settings.json" << 'EOF'
{"auto_start_rpc": true, "remote_enabled": false}
EOF

# 4. Start Xvfb (virtual display for FreeCAD GUI)
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &

# 5. Start FreeCAD with the addon
#    - Unset WAYLAND_DISPLAY to force X11 (Wayland breaks Xvfb rendering)
#    - Use -M to load addon without symlinking into Mod/
unset WAYLAND_DISPLAY
DISPLAY=:99 HOME=$FREECAD_USER_HOME FREECAD_USER_HOME=$FREECAD_USER_HOME \
  QT_QPA_PLATFORM=xcb \
  freecad -M /tmp/freecad-mcp/addon/FreeCADMCP &

# 6. Wait for XML-RPC server (takes ~4-7 seconds)
python3 << 'PYEOF'
import xmlrpc.client, time
s = xmlrpc.client.ServerProxy("http://localhost:9875")
for i in range(60):
    try:
        s.ping()
        print(f"FreeCAD RPC ready after {i * 0.5:.1f}s")
        break
    except Exception:
        time.sleep(0.5)
else:
    raise RuntimeError("FreeCAD RPC did not start within 30s")
PYEOF
```

### Executing code

```bash
python3 -c "
import xmlrpc.client, json
s = xmlrpc.client.ServerProxy('http://localhost:9875')
print(json.dumps(s.execute_code('''
import FreeCAD
import Part
doc = FreeCAD.newDocument(\"Demo\")
box = doc.addObject(\"Part::Box\", \"MyBox\")
box.Length = 50; box.Width = 30; box.Height = 20
doc.recompute()
print(f\"Objects: {[o.Name for o in doc.Objects]}\")
'''), indent=2))
"
```

The `execute_code` response is `{"success": bool, "message": str}`. The `message` includes
captured stdout from `print()` calls in the code.

Variables and documents persist across calls — a second `execute_code` can reference objects
created in the first.

### Capturing and viewing screenshots

```bash
python3 -c "
import xmlrpc.client, base64
s = xmlrpc.client.ServerProxy('http://localhost:9875')
img = s.get_active_screenshot('Isometric', 800, 600, '')
with open('/tmp/freecad_view.png', 'wb') as f:
    f.write(base64.b64decode(img))
print('Screenshot saved')
"
```

Then use `Read(file_path="/tmp/freecad_view.png")` to view the image. The Read tool renders
images natively — this closes the feedback loop: send code, view result, adjust, repeat.

**View options** for `get_active_screenshot(view_name, width, height, focus_object)`:

- `view_name`: `"Isometric"`, `"Front"`, `"Top"`, `"Right"`, `"Back"`, `"Left"`, `"Bottom"`,
  `"Dimetric"`, `"Trimetric"`
- `focus_object`: object name to zoom to, or `""` for fit-all

### Other XML-RPC methods

```python
s.ping()                          # → True (health check)
s.create_document("name")         # → {"success": bool, "document_name": str}
s.list_documents()                # → ["Doc1", "Doc2", ...]
s.get_objects("DocName")          # → [{"Name": ..., "TypeId": ..., "Properties": ...}, ...]
s.get_object("DocName", "ObjName") # → {"Name": ..., "Properties": ...}
```

### Gotchas

- **Wireframe only under Xvfb**: screenshots render as wireframe (no GPU for OpenGL
  shading). Wireframe is sufficient for checking geometry topology and dimensions.
- **Port 9875 is hardcoded** in the addon — only one FreeCAD instance per machine.
- **Wayland override**: must unset `WAYLAND_DISPLAY` and set `QT_QPA_PLATFORM=xcb` when
  the host desktop uses Wayland, otherwise FreeCAD connects to Wayland instead of Xvfb.
- **Settings path**: `$FREECAD_USER_HOME/freecad_mcp_settings.json` (directly in the user
  home directory, not in `.config/FreeCAD/`).

### Shutdown

```bash
# Kill FreeCAD and Xvfb
kill %2 %1  # or by PID
```
