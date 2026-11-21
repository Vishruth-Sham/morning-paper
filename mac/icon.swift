import AppKit
let s: CGFloat = 1024
let img = NSImage(size: NSSize(width: s, height: s))
img.lockFocus()
let rect = NSRect(x: 100, y: 100, width: 824, height: 824)
NSColor(red: 0xf0/255, green: 0xee/255, blue: 0xe9/255, alpha: 1).setFill()
NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185).fill()
NSColor.black.setFill()
NSRect(x: 220, y: 760, width: 584, height: 14).fill()
NSRect(x: 220, y: 250, width: 584, height: 14).fill()
let font = NSFont(name: "Georgia-Bold", size: 400) ?? NSFont(name: "Times New Roman Bold", size: 400)!
let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
let str = NSAttributedString(string: "MP", attributes: attrs)
let sz = str.size()
str.draw(at: NSPoint(x: (s - sz.width)/2, y: (s - sz.height)/2 + 10))
img.unlockFocus()
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
rep.size = NSSize(width: s, height: s)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
img.draw(in: NSRect(x: 0, y: 0, width: s, height: s))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
