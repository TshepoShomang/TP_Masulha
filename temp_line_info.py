from pathlib import Path
text = Path("server.py").read_text().splitlines()
for idx, line in enumerate(text, 1):
    if '@app.route("/reset_password"' in line:
        print('reset route', idx, line)
