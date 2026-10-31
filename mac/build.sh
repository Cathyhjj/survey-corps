#!/bin/bash
# Builds "Survey Corps.app" and installs it to ~/Applications. Run: bash mac/build.sh
set -euo pipefail
cd "$(dirname "$0")"
B=build; APP="$B/Survey Corps.app"
rm -rf "$B"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$B/AppIcon.iconset"

swiftc -O "$PWD/SurveyCorps.swift" -o "$APP/Contents/MacOS/SurveyCorps"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>SurveyCorps</string>
  <key>CFBundleIdentifier</key><string>local.mailtodo</string>
  <key>CFBundleName</key><string>Survey Corps</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
EOF

# Icon: mac/icon-source.png (any image, not in git) on a green rounded square, sized to match the Dock's other icons. Skipped if missing.
cat > "$B/icon.swift" <<'EOF'
import AppKit
let src = NSImage(contentsOfFile: CommandLine.arguments[1])!
func rgb(_ v: Int) -> NSColor { NSColor(srgbRed: CGFloat(v >> 16 & 255) / 255, green: CGFloat(v >> 8 & 255) / 255, blue: CGFloat(v & 255) / 255, alpha: 1) }
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8,
    samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSGraphicsContext.current!.imageInterpolation = .high
func shadow(_ blur: CGFloat, _ y: CGFloat, _ a: CGFloat) { let s = NSShadow(); s.shadowBlurRadius = blur; s.shadowOffset = NSSize(width: 0, height: y); s.shadowColor = .black.withAlphaComponent(a); s.set() }
let shape = NSBezierPath(roundedRect: NSRect(x: 88, y: 88, width: 848, height: 848), xRadius: 190, yRadius: 190)
NSGraphicsContext.saveGraphicsState(); shadow(20, -10, 0.3); rgb(0x1d3a27).setFill(); shape.fill(); NSGraphicsContext.restoreGraphicsState()
NSGradient(starting: rgb(0x3f6b4a), ending: rgb(0x1d3a27))!.draw(in: shape, angle: -90)  // Survey Corps cloak green
let box: CGFloat = 618, k = min(box / src.size.width, box / src.size.height)
let w = src.size.width * k, h = src.size.height * k
NSGraphicsContext.saveGraphicsState(); shadow(14, -6, 0.45)
src.draw(in: NSRect(x: (1024 - w) / 2, y: (1024 - h) / 2, width: w, height: h))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
EOF
if [ -f icon-source.png ]; then
swiftc "$B/icon.swift" -o "$B/icon" && "$B/icon" icon-source.png "$B/icon.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$B/icon.png" --out "$B/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$B/icon.png" --out "$B/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
ICON="AppIcon-$(shasum "$B/icon.png" | cut -c1-8)"  # new name per image: macOS caches icons by file and ignores a changed one
iconutil -c icns "$B/AppIcon.iconset" -o "$APP/Contents/Resources/$ICON.icns"
plutil -replace CFBundleIconFile -string "$ICON" "$APP/Contents/Info.plist"
fi

# Signed with a fixed self-made certificate (if you made one), macOS keeps the app's Screen Recording permission
# (Screenshot to Task) across rebuilds; ad-hoc signing loses it every build.
# Make it once: Keychain Access → Certificate Assistant → Create a Certificate…, name "Survey Corps Signing",
# Identity Type "Self Signed Root", Certificate Type "Code Signing".
SIGN="Survey Corps Signing"
if security find-certificate -c "$SIGN" >/dev/null 2>&1; then
  codesign --force --deep -s "$SIGN" "$APP" && echo "Signed with \"$SIGN\""
fi

mkdir -p ~/Applications
rm -rf ~/Applications/"Survey Corps.app"
cp -R "$APP" ~/Applications/
echo "Installed ~/Applications/Survey Corps.app"
