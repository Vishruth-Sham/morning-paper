#!/bin/bash
# Bring an already-open Morning Paper tab to the front (and reload it); open a new one only if none exists.
# The first time, macOS asks to let "Morning Paper" control your browser - click OK.
URL="${1:-http://127.0.0.1:5055/}"
MATCH="127.0.0.1:5055"

running() { osascript -e "application \"$1\" is running" 2>/dev/null | grep -q true; }

for B in "Google Chrome" "Brave Browser" "Microsoft Edge" "Arc" "Chromium"; do
  running "$B" || continue
  r=$(osascript 2>/dev/null <<AS
tell application "$B"
  repeat with w in windows
    set i to 0
    repeat with t in tabs of w
      set i to i + 1
      if URL of t contains "$MATCH" then
        set active tab index of w to i
        set index of w to 1
        tell t to reload
        activate
        return "found"
      end if
    end repeat
  end repeat
end tell
return "none"
AS
)
  [ "$r" = "found" ] && exit 0
done

if running "Safari"; then
  r=$(osascript 2>/dev/null <<AS
tell application "Safari"
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t contains "$MATCH" then
        set current tab of w to t
        set index of w to 1
        set URL of t to (URL of t)
        activate
        return "found"
      end if
    end repeat
  end repeat
end tell
return "none"
AS
)
  [ "$r" = "found" ] && exit 0
fi

open "$URL"
