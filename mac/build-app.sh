#!/bin/bash
# Builds "~/Applications/Morning Paper.app": one click starts the server (if needed),
# makes today's edition if it doesn't exist yet, and opens the paper in the browser.
# Usage: mac/build-app.sh [--dock]
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)" || { echo "uv not found on PATH" >&2; exit 1; }
APP="$HOME/Applications/Morning Paper.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Morning Paper</string>
  <key>CFBundleDisplayName</key><string>Morning Paper</string>
  <key>CFBundleIdentifier</key><string>local.morningpaper.app</string>
  <key>CFBundleExecutable</key><string>MorningPaper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/MorningPaper" <<LAUNCHER
#!/bin/bash
UV="$UV"
export PATH="$(dirname "$UV"):/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$PROJECT" || exit 1
URL="http://127.0.0.1:5055"
if ! curl -s -o /dev/null "\$URL/"; then
  nohup "\$UV" run morning-paper >> data-server.log 2>&1 &
  for i in \$(seq 1 30); do
    sleep 1
    curl -s -o /dev/null "\$URL/" && break
  done
fi
# Starts today's edition only if it doesn't exist yet
curl -s -X POST "\$URL/generate" -o /dev/null
"$PROJECT/mac/focus-or-open.sh" "\$URL/"   # reuse an open tab instead of opening another
LAUNCHER
chmod +x "$APP/Contents/MacOS/MorningPaper"

# Icon (optional: skipped if swift isn't available)
TMP="$(mktemp -d)"
if swift "$PROJECT/mac/icon.swift" "$TMP/icon1024.png" 2>/dev/null; then
  mkdir "$TMP/AppIcon.iconset"
  for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$TMP/icon1024.png" --out "$TMP/AppIcon.iconset/icon_${sz}x${sz}.png" >/dev/null
    d=$((sz*2)); sips -z $d $d "$TMP/icon1024.png" --out "$TMP/AppIcon.iconset/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
fi
rm -rf "$TMP"

xattr -cr "$APP"
touch "$APP"
echo "Built $APP"

if [ "${1:-}" = "--dock" ] && ! defaults read com.apple.dock persistent-apps | grep -q "Morning"; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$APP</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
  killall Dock
  echo "Added to Dock"
fi
