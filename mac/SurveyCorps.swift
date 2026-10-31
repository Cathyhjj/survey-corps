// Native window around app.py. Handles Outlook drags itself (file promises), which browsers don't reliably do.
import Cocoa
import WebKit

// app.py is found relative to this source file, so rebuild if you move the project.
let appPy = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("app.py").path
let base = URL(string: "http://127.0.0.1:8765")!
let settingsURL = base.appendingPathComponent("settings")
let petSize: CGFloat = 112  // pet panel size in points: a 96 pt pet plus room to hop and lean
let sliver: CGFloat = 40    // how much of a tucked pet stays on screen, drawn as a tab

// POST to app.py; done runs on the main thread with the body and status (0 = server unreachable).
func post(_ path: String, _ body: Any? = nil, raw: Data? = nil, query: [URLQueryItem] = [],
          done: @escaping (Data?, Int) -> Void = { _, _ in }) {
    var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
    if !query.isEmpty { c.queryItems = query }
    var req = URLRequest(url: c.url!)
    req.httpMethod = "POST"
    req.timeoutInterval = 150  // Argo answers can take a minute or two
    req.httpBody = raw ?? body.flatMap { try? JSONSerialization.data(withJSONObject: $0) }
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        DispatchQueue.main.async { done(data, code) }
    }.resume()
}
func json(_ d: Data?) -> [String: Any] { d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:] }

// Dropped files: real file URLs, or Outlook's file promises (written to a temp folder first).
func receiveFiles(_ pb: NSPasteboard, each: @escaping (URL) -> Void) {
    if let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
        urls.forEach(each)
    } else if let promises = pb.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver] {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for p in promises {
            p.receivePromisedFiles(atDestination: dir, options: [:], operationQueue: .main) { url, err in
                if err == nil { each(url) }
            }
        }
    }
}

// An email (or any file) becomes a task; done gets its title, nil on failure.
func uploadEmail(_ file: URL, done: @escaping (String?) -> Void = { _ in }) {
    guard let data = try? Data(contentsOf: file) else { return done(nil) }
    post("api/drop", raw: data, query: [URLQueryItem(name: "name", value: file.lastPathComponent)]) { d, code in
        done(code == 200 ? json(d)["title"] as? String ?? file.lastPathComponent : nil)
    }
}

// One-line text box in an alert. nil when cancelled or left blank.
func textPrompt(_ title: String, info: String = "", text: String = "", placeholder: String = "") -> String? {
    NSApp.activate(ignoringOtherApps: true)  // pets don't activate the app; the alert needs it
    let a = NSAlert(); a.messageText = title; a.informativeText = info
    a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
    let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 24))
    tf.stringValue = text; tf.placeholderString = placeholder
    a.accessoryView = tf
    a.window.initialFirstResponder = tf
    guard a.runModal() == .alertFirstButtonReturn else { return nil }
    let v = tf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return v.isEmpty ? nil : v
}

class DropWebView: WKWebView {
    static let promiseTypes = NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) }

    override init(frame: NSRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frame, configuration: configuration)
        registerForDraggedTypes(registeredDraggedTypes + Self.promiseTypes + [.fileURL])
    }
    required init?(coder: NSCoder) { fatalError() }

    func isFile(_ s: NSDraggingInfo) -> Bool {
        let types = s.draggingPasteboard.types ?? []
        return types.contains(.fileURL) || types.contains(where: Self.promiseTypes.contains)
    }
    // super still runs so the page shows its drop highlight
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation {
        let op = super.draggingEntered(s); return isFile(s) ? .copy : op
    }
    override func draggingUpdated(_ s: NSDraggingInfo) -> NSDragOperation {
        let op = super.draggingUpdated(s); return isFile(s) ? .copy : op
    }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool {
        guard isFile(s) else { return super.performDragOperation(s) }
        let pb = s.draggingPasteboard
        receiveFiles(pb) { uploadEmail($0) { _ in self.evaluateJavaScript("drop.classList.remove('over');load()") } }
        return true
    }
}

// Erwin, the pet: an original plush chibi with swept-back blond hair, thick brows, square jaw, stern look, holding a letter. 64x64 box.
func drawPet(_ b: NSRect) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func oval(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSBezierPath {
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: w, height: h))
    }
    let hair = rgb(250, 226, 130), hairDark = rgb(214, 178, 72), skin = rgb(248, 216, 192), skinEdge = rgb(222, 180, 152)
    let ink = rgb(35, 28, 26)
    let sh = NSShadow(); sh.shadowBlurRadius = 3; sh.shadowOffset = NSSize(width: 0, height: -1)
    sh.shadowColor = NSColor.black.withAlphaComponent(0.35)

    // back of the hair
    NSGraphicsContext.saveGraphicsState(); sh.set()
    let back = NSBezierPath(roundedRect: NSRect(x: 7, y: 20, width: 50, height: 42), xRadius: 23, yRadius: 22)
    hair.setFill(); back.fill()
    NSGraphicsContext.restoreGraphicsState()

    // wide plush face
    let face = NSBezierPath(roundedRect: NSRect(x: 12, y: 15, width: 40, height: 35), xRadius: 16, yRadius: 15)
    skin.setFill(); face.fill(); skinEdge.setStroke(); face.lineWidth = 0.8; face.stroke()

    // eyes: large, round, thick upper lash, glossy blue iris
    for (cx, dir): (CGFloat, CGFloat) in [(23.5, -1), (40.5, 1)] {
        let eye = NSBezierPath(roundedRect: NSRect(x: cx - 5, y: 25.5, width: 10, height: 10), xRadius: 4.8, yRadius: 4.8)
        NSColor.white.setFill(); eye.fill()
        NSGraphicsContext.saveGraphicsState(); eye.addClip()
        let iris = oval(cx - 4.3, 25.5, 8.6, 9.6)
        NSGradient(starting: rgb(15, 70, 160), ending: rgb(80, 185, 245))!.draw(in: iris, angle: -90)
        rgb(10, 45, 110).setStroke(); iris.lineWidth = 0.7; iris.stroke()
        rgb(15, 40, 95, 0.8).setFill(); oval(cx - 2, 28.5, 4, 4.4).fill()
        NSColor.white.setFill(); oval(cx - dir * 1.4 - 1.5, 30.6, 3, 3).fill()      // main highlight
        NSColor.white.withAlphaComponent(0.9).setFill(); oval(cx + dir * 1.6 - 0.7, 27.0, 1.4, 1.4).fill()
        NSGraphicsContext.restoreGraphicsState()
        let lash = NSBezierPath(); lash.lineWidth = 1.9; lash.lineCapStyle = .round; lash.lineJoinStyle = .round
        lash.move(to: NSPoint(x: cx - dir * 4.6, y: 33.3))
        lash.curve(to: NSPoint(x: cx + dir * 5.4, y: 33.6), controlPoint1: NSPoint(x: cx - dir * 2.5, y: 36.2),
                   controlPoint2: NSPoint(x: cx + dir * 3, y: 36.4))
        lash.line(to: NSPoint(x: cx + dir * 6.2, y: 32.2))
        ink.setStroke(); lash.stroke()
        let lower = NSBezierPath(); lower.move(to: NSPoint(x: cx - 2.5, y: 25.2)); lower.line(to: NSPoint(x: cx + 2.5, y: 25.2))
        rgb(120, 80, 70, 0.5).setStroke(); lower.lineWidth = 0.6; lower.stroke()
    }

    // mouth: small, flat, serious
    let mouth = NSBezierPath(); mouth.move(to: NSPoint(x: 29.8, y: 21.6))
    mouth.curve(to: NSPoint(x: 34.2, y: 21.6), controlPoint1: NSPoint(x: 31, y: 21.7), controlPoint2: NSPoint(x: 33, y: 21.7))
    ink.setStroke(); mouth.lineWidth = 0.9; mouth.lineCapStyle = .round; mouth.stroke()

    // swept-back hair with a side part and a high hairline; short sideburns
    let top = NSBezierPath(); top.move(to: NSPoint(x: 8, y: 40))
    top.curve(to: NSPoint(x: 32, y: 62), controlPoint1: NSPoint(x: 7, y: 56), controlPoint2: NSPoint(x: 18, y: 62))
    top.curve(to: NSPoint(x: 56, y: 40), controlPoint1: NSPoint(x: 46, y: 62), controlPoint2: NSPoint(x: 57, y: 56))
    for (x, y): (CGFloat, CGFloat) in [(54, 30), (50, 32), (49, 43)] { top.line(to: NSPoint(x: x, y: y)) }
    top.curve(to: NSPoint(x: 15, y: 43), controlPoint1: NSPoint(x: 40, y: 49), controlPoint2: NSPoint(x: 24, y: 49))
    for (x, y): (CGFloat, CGFloat) in [(14, 32), (10, 30)] { top.line(to: NSPoint(x: x, y: y)) }
    top.close(); top.lineJoinStyle = .round
    hair.setFill(); top.fill(); hairDark.setStroke(); top.lineWidth = 0.9; top.stroke()
    let strands: [(CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat)] = [
        (29, 59, 49, 52, 36, 61, 44, 58), (27, 54, 51, 44, 34, 57, 44, 52), (25, 50.5, 50, 38, 33, 52, 43, 47),
        (22, 58, 10, 48, 15, 59, 10, 54), (21, 52, 11, 41, 15, 53, 11, 47)]
    for (x0, y0, x1, y1, c1x, c1y, c2x, c2y) in strands {
        let st = NSBezierPath(); st.move(to: NSPoint(x: x0, y: y0))
        st.curve(to: NSPoint(x: x1, y: y1), controlPoint1: NSPoint(x: c1x, y: c1y), controlPoint2: NSPoint(x: c2x, y: c2y))
        hairDark.withAlphaComponent(0.6).setStroke(); st.lineWidth = 0.8; st.lineCapStyle = .round; st.stroke()
    }
    let part = NSBezierPath(); part.move(to: NSPoint(x: 23.5, y: 47))
    part.curve(to: NSPoint(x: 28, y: 61), controlPoint1: NSPoint(x: 23, y: 53), controlPoint2: NSPoint(x: 25, y: 58))
    hairDark.setStroke(); part.lineWidth = 1.2; part.lineCapStyle = .round; part.stroke()
    let shine = NSBezierPath(); shine.move(to: NSPoint(x: 33, y: 58.5))
    shine.curve(to: NSPoint(x: 45, y: 54), controlPoint1: NSPoint(x: 37, y: 60), controlPoint2: NSPoint(x: 42, y: 57.5))
    NSColor.white.withAlphaComponent(0.6).setStroke(); shine.lineWidth = 1.4; shine.lineCapStyle = .round; shine.stroke()
    // thick, flat, stern brows
    for (a, c): ((CGFloat, CGFloat), (CGFloat, CGFloat)) in [((17.5, 38.8), (28.5, 37.6)), ((46.5, 38.8), (35.5, 37.6))] {
        let br = NSBezierPath(); br.move(to: NSPoint(x: a.0, y: a.1)); br.line(to: NSPoint(x: c.0, y: c.1))
        rgb(120, 88, 36).setStroke(); br.lineWidth = 3; br.lineCapStyle = .round; br.stroke()
    }
    // small nose and chin dimple
    let nose = NSBezierPath(); nose.move(to: NSPoint(x: 32, y: 27)); nose.line(to: NSPoint(x: 33, y: 24.6))
    skinEdge.setStroke(); nose.lineWidth = 0.9; nose.lineCapStyle = .round; nose.stroke()
    let dimple = NSBezierPath(); dimple.move(to: NSPoint(x: 30.8, y: 18)); dimple.line(to: NSPoint(x: 33.2, y: 18))
    skinEdge.setStroke(); dimple.lineWidth = 0.8; dimple.lineCapStyle = .round; dimple.stroke()

    // the letter, held with two little hands
    NSGraphicsContext.saveGraphicsState(); sh.set()
    let env = NSBezierPath(roundedRect: NSRect(x: 15, y: 1, width: 34, height: 16), xRadius: 3, yRadius: 3)
    rgb(255, 250, 240).setFill(); env.fill()
    NSGraphicsContext.restoreGraphicsState()
    rgb(40, 96, 190).setStroke(); env.lineWidth = 1.4; env.stroke()
    let flap = NSBezierPath(); flap.move(to: NSPoint(x: 16, y: 15.5)); flap.line(to: NSPoint(x: 32, y: 7.5)); flap.line(to: NSPoint(x: 48, y: 15.5))
    flap.lineWidth = 1.5; flap.lineJoinStyle = .round; flap.stroke()
    for x: CGFloat in [11.5, 45.5] {
        let hand = oval(x, 5, 7, 7); skin.setFill(); hand.fill(); skinEdge.setStroke(); hand.lineWidth = 0.9; hand.stroke()
    }
}

// Busy animation over the pet, in drawPet's 64-unit space at time t (s): little letters rise out of the envelope,
// one to each side, and turn into green ticks.
func drawWork(_ b: NSRect, t: Double) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func life(_ period: Double, _ offset: Double = 0) -> CGFloat { CGFloat((t / period + offset).truncatingRemainder(dividingBy: 1)) }
    for (i, side) in [(0, 1.0), (1, -1.0)] as [(Int, CGFloat)] {
        let p = life(1.8, Double(i) / 2), x = 32 + side * (17 + 13 * p), y = 13 + 26 * p - 12 * p * p  // up past the cheeks, clear of the face
        if p < 0.6 {
            let a = min(1, p * 6), r = NSRect(x: x - 3.5, y: y - 2.2, width: 7, height: 4.6)
            rgb(255, 250, 240, a).setFill(); NSBezierPath(roundedRect: r, xRadius: 0.8, yRadius: 0.8).fill()
            let v = NSBezierPath(); v.move(to: NSPoint(x: r.minX + 0.6, y: r.maxY - 0.6)); v.line(to: NSPoint(x: r.midX, y: r.midY - 0.3))
            v.line(to: NSPoint(x: r.maxX - 0.6, y: r.maxY - 0.6))
            rgb(40, 96, 190, a).setStroke(); v.lineWidth = 0.8; v.stroke(); NSBezierPath(roundedRect: r, xRadius: 0.8, yRadius: 0.8).stroke()
        } else {
            let a = 1 - (p - 0.6) / 0.4, tick = NSBezierPath()
            tick.move(to: NSPoint(x: x - 2.6, y: y)); tick.line(to: NSPoint(x: x - 0.6, y: y - 2)); tick.line(to: NSPoint(x: x + 3, y: y + 2.4))
            rgb(52, 180, 90, a).setStroke(); tick.lineWidth = 1.8; tick.lineCapStyle = .round; tick.lineJoinStyle = .round; tick.stroke()
        }
    }
}

// Menu item that runs a closure.
class Item: NSMenuItem {
    var run: () -> Void = {}
    convenience init(_ title: String, enabled: Bool = true, _ run: @escaping () -> Void = {}) {
        self.init(title: title, action: #selector(fire), keyEquivalent: "")
        self.run = run; target = self; isEnabled = enabled
    }
    @objc func fire() { run() }
}

// Floating pet that stays on every desktop. Click: show/hide the window. Drag: move it. Drop things on it.
// Right-click: its menu. A "…" thought bubble while Argo works.
class PetView: NSView {
    var onClick: () -> Void = {}
    var onDrop: (NSPasteboard) -> Bool = { _ in false }
    var onMoved: () -> Void = {}
    var makeMenu: () -> NSMenu = { NSMenu() }
    var grab = NSPoint.zero, moved = false
    var overdue = 0 { didSet { if overdue != oldValue { needsDisplay = true } } }  // red badge: open tasks past their due time
    var hover = false { didSet { if hover != oldValue { needsDisplay = true } } }  // something dragged over it
    var busy = false { didSet { if busy != oldValue { animate() } } }
    var tuckedInto: NSRectEdge? { didSet { if tuckedInto != oldValue { needsDisplay = true } } }  // the screen edge it's tucked into
    enum Anim { case hop, shake }  // hop: a drop worked or the pet speaks; shake: it didn't
    var anim: Anim?, animStart = Date(), ticker: Timer?
    func play(_ a: Anim) { anim = a; animStart = Date(); animate() }
    func animate() {  // redraw at 30 fps while something moves, stop when nothing does
        needsDisplay = true
        guard ticker == nil, busy || anim != nil else { return }
        ticker = Timer.scheduledTimer(withTimeInterval: 1 / 30, repeats: true) { [weak self] t in
            guard let self else { return t.invalidate() }
            self.needsDisplay = true
            if !self.busy && self.anim == nil { t.invalidate(); self.ticker = nil }
        }
    }
    override func draw(_ r: NSRect) {
        // Fully transparent pixels let clicks fall through; a near-invisible fill makes the whole square clickable.
        NSColor.black.withAlphaComponent(0.01).setFill(); bounds.fill()
        if let e = tuckedInto { return drawTab(e) }
        // pose: lean in while something is dragged over, hop / shake once, bob while busy
        let now = Date().timeIntervalSinceReferenceDate, t = Date().timeIntervalSince(animStart)
        var dx: CGFloat = 0, dy: CGFloat = 0, grow: CGFloat = hover ? 1.08 : 1
        if let a = anim {
            if t > 0.5 { anim = nil }
            else if a == .hop { dy = 9 * sin(t / 0.5 * .pi) }
            else { dx = 5 * sin(t * 45) * (1 - t / 0.5) }
        }
        if busy { dy += 1.5 * abs(sin(now * 4)) }
        let pet = NSRect(x: (bounds.width - 96) / 2, y: 3, width: 96, height: 96)
        NSGraphicsContext.saveGraphicsState()
        let cg = NSGraphicsContext.current!.cgContext
        cg.translateBy(x: pet.midX + dx, y: pet.minY + dy)  // pivot at the feet
        cg.scaleBy(x: grow, y: grow)
        cg.translateBy(x: -48, y: 0)
        cg.saveGState(); drawPet(NSRect(x: 0, y: 0, width: 96, height: 96)); cg.restoreGState()
        if busy { drawWork(NSRect(x: 0, y: 0, width: 96, height: 96), t: now) }
        NSGraphicsContext.restoreGraphicsState()
        let phase = Int(now / 0.35)
        if hover {
            let ring = NSBezierPath(roundedRect: bounds.insetBy(dx: 2, dy: 2), xRadius: 20, yRadius: 20)
            NSColor.controlAccentColor.setStroke(); ring.lineWidth = 3; ring.stroke()
        }
        if busy {
            let t = NSRect(x: 3, y: bounds.maxY - 25, width: 34, height: 20)
            let b = NSBezierPath(roundedRect: t, xRadius: 10, yRadius: 10)
            b.appendOval(in: NSRect(x: t.minX + 15, y: t.minY - 6, width: 5, height: 5))  // little thought puff below
            NSColor.white.withAlphaComponent(0.95).setFill(); b.fill(); NSColor.gray.setStroke(); b.lineWidth = 1; b.stroke()
            for i in 0..<3 {
                (phase % 3 == i ? NSColor.darkGray : NSColor(white: 0.78, alpha: 1)).setFill()
                NSBezierPath(ovalIn: NSRect(x: t.minX + 7 + CGFloat(i) * 8, y: t.midY - 2.5, width: 5, height: 5)).fill()
            }
        }
        guard overdue > 0 else { return }
        let text = NSAttributedString(string: overdue > 99 ? "99+" : "\(overdue)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.white])
        let w = max(26, text.size().width + 12), r = NSRect(x: bounds.maxX - w - 2, y: bounds.maxY - 28, width: w, height: 26)
        let pill = NSBezierPath(roundedRect: r, xRadius: 13, yRadius: 13)
        NSColor.systemRed.setFill(); pill.fill(); NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
        text.draw(at: NSPoint(x: r.midX - text.size().width / 2, y: r.midY - text.size().height / 2))
    }
    var tabLong: CGFloat { overdue > 0 ? 72 : 40 }  // a tab's length along the edge: face, then count
    func drawTab(_ e: NSRectEdge) {  // tucked: a tab in the sliver left showing, the pet's face and its count
        let side = e == .minX || e == .maxX, long = tabLong
        let a = e == .minX ? bounds.maxX - sliver : e == .minY ? bounds.maxY - sliver : 0  // where the on-screen strip starts
        var tab = side ? NSRect(x: a + 1, y: bounds.midY - long / 2, width: sliver - 2, height: long)
                       : NSRect(x: bounds.midX - long / 2, y: a + 1, width: long, height: sliver - 2)
        switch e {  // run the tab off the screen edge, so only its inner corners are round
        case .minX: tab.origin.x -= 12; tab.size.width += 12
        case .maxX: tab.size.width += 12
        case .minY: tab.origin.y -= 12; tab.size.height += 12
        default: tab.size.height += 12
        }
        let shape = NSBezierPath(roundedRect: tab, xRadius: 14, yRadius: 14)
        NSGraphicsContext.saveGraphicsState()
        let sh = NSShadow(); sh.shadowBlurRadius = 3; sh.shadowOffset = NSSize(width: 0, height: -1)
        sh.shadowColor = NSColor.black.withAlphaComponent(0.3); sh.set()
        NSColor(white: 0.98, alpha: 1).setFill(); shape.fill()
        NSGraphicsContext.restoreGraphicsState()
        NSColor(white: 0, alpha: 0.15).setStroke(); shape.lineWidth = 1; shape.stroke()
        // the face first (top of a side tab, left of a bottom/top one), the count after it
        let c = side ? NSPoint(x: a + sliver / 2, y: bounds.midY + long / 2 - 20) : NSPoint(x: bounds.midX - long / 2 + 20, y: a + sliver / 2)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(ovalIn: NSRect(x: c.x - 16, y: c.y - 16, width: 32, height: 32)).addClip()
        let cg = NSGraphicsContext.current!.cgContext, k: CGFloat = 0.67  // the 64-unit pet at this scale: its head fills the circle
        cg.translateBy(x: c.x - 32 * k, y: c.y - 43 * k)
        drawPet(NSRect(x: 0, y: 0, width: 64 * k, height: 64 * k))
        NSGraphicsContext.restoreGraphicsState()
        guard overdue > 0 else { return }
        let text = NSAttributedString(string: overdue > 99 ? "99+" : "\(overdue)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: overdue > 99 ? 10 : overdue > 9 ? 12 : 14), .foregroundColor: NSColor.white])
        let d = side ? NSPoint(x: c.x, y: c.y - 33) : NSPoint(x: c.x + 33, y: c.y)
        let dot = NSBezierPath(ovalIn: NSRect(x: d.x - 13, y: d.y - 13, width: 26, height: 26))
        NSColor.systemRed.setFill(); dot.fill()
        text.draw(at: NSPoint(x: d.x - text.size().width / 2, y: d.y - text.size().height / 2))
    }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func rightMouseDown(with e: NSEvent) { NSMenu.popUpContextMenu(makeMenu(), with: e, for: self) }
    override func mouseDown(with e: NSEvent) {
        grab = e.locationInWindow; moved = false
        if e.modifierFlags.contains(.control) { moved = true; rightMouseDown(with: e) }  // ctrl-click = right-click
    }
    override func mouseDragged(with e: NSEvent) {
        let p = NSEvent.mouseLocation, at = convert(e.locationInWindow, from: nil), from = convert(grab, from: nil)
        if !moved && hypot(at.x - from.x, at.y - from.y) < 4 { return }  // a shaky click is still a click
        moved = true
        window?.setFrameOrigin(NSPoint(x: p.x - grab.x, y: p.y - grab.y))
    }
    override func mouseUp(with e: NSEvent) {
        if moved { keepOnScreen(); onMoved() } else { onClick() }
    }
    func keepOnScreen() {  // all of the pet stays clickable: pull it back inside the screen, snap to an edge within 24 pt
        if let w = window { w.setFrameOrigin(onScreen(w.frame)) }
    }
    static let dragTypes = DropWebView.promiseTypes + [.fileURL, .URL, .string]
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation { hover = true; return .copy }
    override func draggingExited(_ s: NSDraggingInfo?) { hover = false }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool { hover = false; return onDrop(s.draggingPasteboard) }
}

func screenOf(_ r: NSRect) -> NSScreen {  // the screen the pet's rect is centered on (or the main one, if off every screen)
    NSScreen.screens.first { $0.frame.contains(NSPoint(x: r.midX, y: r.midY)) } ?? NSScreen.main!
}
// Pet at r tucked into an edge of its screen (frame f, visible v), a sliver left showing: its nearer side, else
// the other side, else the bottom, else the top. An edge another screen touches (others) is skipped: the pet would
// show up on that screen instead. Stays at r if every edge is shared.
func tuckSpot(_ r: NSRect, _ f: NSRect, _ v: NSRect, _ others: [NSRect]) -> NSRect {
    let w = r.width, h = r.height, sides = [  // (tucked rect, a 1 pt strip just past that edge)
        (NSRect(x: v.minX - w + sliver, y: r.minY, width: w, height: h), NSRect(x: f.minX - 1, y: r.minY, width: 1, height: h)),
        (NSRect(x: v.maxX - sliver, y: r.minY, width: w, height: h), NSRect(x: f.maxX, y: r.minY, width: 1, height: h)),
        (NSRect(x: r.minX, y: v.minY - h + sliver, width: w, height: h), NSRect(x: r.minX, y: f.minY - 1, width: w, height: 1)),
        (NSRect(x: r.minX, y: v.maxY - sliver, width: w, height: h), NSRect(x: r.minX, y: f.maxY, width: w, height: 1))]
    let order = r.midX < v.midX ? [0, 1, 2, 3] : [1, 0, 2, 3]
    return order.map { sides[$0] }.first { side in !others.contains { $0.intersects(side.1) } }?.0 ?? r
}
func onScreen(_ r: NSRect) -> NSPoint {  // r pulled back inside its screen, snapped to an edge within 24 pt
    let v = screenOf(r).visibleFrame
    func fit(_ a: CGFloat, _ lo: CGFloat, _ hi: CGFloat) -> CGFloat { a - lo < 24 ? lo : hi - a < 24 ? hi : a }
    return NSPoint(x: fit(r.minX, v.minX, v.maxX - r.width), y: fit(r.minY, v.minY, v.maxY - r.height))
}

// Speech bubble above the pet: the morning briefing, answers, what a drop added. Click: open the window.
class Bubble: NSPanel {
    var onClick: () -> Void = {}
    var hideTimer: Timer?
    let label = NSTextField(wrappingLabelWithString: "")
    let view = BubbleView()
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        backgroundColor = .clear; isOpaque = false; hasShadow = true
        view.bubble = self
        view.addSubview(label)
        contentView = view
    }
    func show(_ title: String, _ text: String, over pet: NSWindow) {
        let t = NSMutableAttributedString(string: title.isEmpty ? "" : title + "\n",
                                          attributes: [.font: NSFont.boldSystemFont(ofSize: 12.5), .foregroundColor: NSColor.labelColor])
        t.append(NSAttributedString(string: String(text.prefix(700)) + (text.count > 700 ? "…" : ""),
                                    attributes: [.font: NSFont.systemFont(ofSize: 12.5), .foregroundColor: NSColor.labelColor]))
        label.attributedStringValue = t
        let s = label.sizeThatFits(NSSize(width: 280, height: 2000)), tail = BubbleView.tail
        label.frame = NSRect(x: 12, y: tail + 9, width: s.width, height: s.height)
        let w = s.width + 24, h = s.height + 18 + tail
        let screen = (pet.screen ?? NSScreen.main!).visibleFrame
        let x = min(max(pet.frame.midX - w / 2, screen.minX + 4), screen.maxX - w - 4)
        let y = min(pet.frame.maxY - 8, screen.maxY - h)
        view.tailX = min(max(pet.frame.midX - x, 18), w - 18)
        setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
        view.needsDisplay = true
        orderFrontRegardless()
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: min(45, 6 + Double(t.length) * 0.05), repeats: false) { [weak self] _ in
            self?.orderOut(nil)
        }
    }
}

class BubbleView: NSView {
    static let tail: CGFloat = 10
    weak var bubble: Bubble?
    var tailX: CGFloat = 40
    override func draw(_ r: NSRect) {
        let t = Self.tail, body = NSRect(x: 0.5, y: t + 0.5, width: bounds.width - 1, height: bounds.height - t - 1)
        let p = NSBezierPath(roundedRect: body, xRadius: 12, yRadius: 12)
        p.move(to: NSPoint(x: tailX - 8, y: t + 1)); p.line(to: NSPoint(x: tailX, y: 0.5)); p.line(to: NSPoint(x: tailX + 8, y: t + 1))
        NSColor.windowBackgroundColor.setFill(); p.fill()
        NSColor.separatorColor.setStroke(); p.lineWidth = 1; p.stroke()
        NSColor.windowBackgroundColor.setFill(); NSRect(x: tailX - 7, y: t, width: 14, height: 2).fill()  // seam between body and tail
    }
    override func hitTest(_ p: NSPoint) -> NSView? { frame.contains(p) ? self : nil }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseUp(with e: NSEvent) { bubble?.orderOut(nil); bubble?.onClick() }
}

class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!, settingsWindow: NSWindow!, settingsWeb: WKWebView!
    var panel: NSPanel!, pet: PetView!, bubble = Bubble(), lastBubble = 0
    let server = Process()

    @objc func openSettings() {  // Survey Corps > Settings… (⌘,): its own window, like any Mac app
        if settingsWindow == nil {
            settingsWeb = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            settingsWeb.navigationDelegate = self
            settingsWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 640),
                                      styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            settingsWindow.title = "Settings"
            settingsWindow.contentView = settingsWeb
            settingsWindow.center()
            settingsWindow.contentMinSize = NSSize(width: 480, height: 300)
            settingsWindow.setFrameAutosaveName("settings-v4")  // new name: the old one remembers a window too short for the hub rows
            settingsWindow.isReleasedWhenClosed = false
            settingsWeb.load(URLRequest(url: settingsURL))
        }
        settingsWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        // the Dock caches app icons; setting it here shows a new icon right away
        if let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") as? String { NSApp.applicationIconImage = NSImage(named: name) }
        let logPath = NSHomeDirectory() + "/Library/Logs/SurveyCorps.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let log = FileHandle(forWritingAtPath: logPath)
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        server.arguments = [appPy, "--no-browser"]
        server.standardOutput = log
        server.standardError = log
        try? server.run()  // if a server is already running, this one exits and we use that one

        let mainConfig = WKWebViewConfiguration()
        mainConfig.userContentController.add(self, name: "shot")  // the page's camera button
        let web = DropWebView(frame: .zero, configuration: mainConfig)
        web.navigationDelegate = self
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Task Manager"
        window.minSize = NSSize(width: 820, height: 480)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("main")
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        web.load(URLRequest(url: base))

        pet = PetView()
        pet.toolTip = "Erwin: your tasks. Click to show/hide, drag to move, drop an email, text or image on him, right-click for more"
        pet.onClick = { [unowned self] in self.toggle(self.window) }
        pet.onDrop = { [unowned self] in self.drop($0) }
        pet.makeMenu = { [unowned self] in self.menu() }
        pet.onMoved = { [unowned self] in self.setHome(self.panel.frame.origin) }
        panel = makePet(pet)
        let saved = UserDefaults.standard.string(forKey: "petHome.task").map(NSPointFromString) ?? panel.frame.origin
        setHome(onScreen(NSRect(origin: saved, size: panel.frame.size)))
        if pref("petsHidden") { setPetsHidden(true) }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [unowned self] _ in self.tick() }
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [unowned self] _ in self.poll() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.poll() }
    }

    // Everything the pet shows comes from one call: its badge, whether Argo is busy, new speech bubbles.
    func poll() {
        URLSession.shared.dataTask(with: URL(string: "\(base)/api/pets?after=\(lastBubble)")!) { data, _, _ in
            let s = json(data)
            guard !s.isEmpty else { return }
            DispatchQueue.main.async {
                self.pet.busy = s["busy"] as? Bool ?? false
                self.pet.overdue = s["overdue"] as? Int ?? 0  // open tasks past their due time
                for b in s["bubbles"] as? [[String: Any]] ?? [] {
                    self.lastBubble = max(self.lastBubble, b["id"] as? Int ?? 0)
                    self.say(b["title"] as? String ?? "", b["text"] as? String ?? "")
                }
            }
        }.resume()
    }

    func say(_ title: String, _ text: String, ok: Bool = true) {
        if pref("petsHidden") { setPetsHidden(false) }  // a hidden pet comes back when it has something to say
        tucked = false; awaySince = Date(); settle(animate: false)  // out at home before the bubble is placed over it
        bubble.onClick = { NSApp.activate(ignoringOtherApps: true); self.window.makeKeyAndOrderFront(nil) }
        bubble.show(title, text, over: panel)
        pet.play(ok ? .hop : .shake)
        if !pref("petsMute") { NSSound(named: ok ? "Glass" : "Funk")?.play() }  // a low thud when something failed
    }

    // Drop onto the pet: emails, image files or text -> a task.
    func drop(_ pb: NSPasteboard) -> Bool {
        let files = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        let text = pb.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !files.isEmpty || pb.types?.contains(where: DropWebView.promiseTypes.contains) == true {
            receiveFiles(pb) { uploadEmail($0) { t in self.say("", t.map { "Added: \($0)" } ?? "Couldn't read that file", ok: t != nil) } }
        } else if !text.isEmpty {
            addTask(text)
        } else { return false }
        return true
    }

    // Hide the app, let the user drag over an area with macOS's own picker (Space: a window, Esc: cancel),
    // then Argo reads the image into a task. First use asks for Screen Recording permission.
    func screenshotTask() {
        let shown = window.isVisible
        NSApp.hide(nil)
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("survey-corps-shot.png")
        try? FileManager.default.removeItem(at: file)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {  // let the window finish hiding first
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-i", "-x", file.path]
            p.terminationHandler = { _ in DispatchQueue.main.async {
                if shown { NSApp.activate(ignoringOtherApps: true); self.window.makeKeyAndOrderFront(nil) }
                guard FileManager.default.fileExists(atPath: file.path) else { return }  // Esc: nothing taken
                uploadEmail(file) { title in
                    self.say("", title != nil ? "Added the screenshot. Argo is reading it." : "Couldn't add the screenshot", ok: title != nil)
                }
            } }
            try? p.run()
        }
    }

    func addTask(_ text: String) {
        post("api/drop", raw: Data(text.utf8)) { d, code in
            self.say("", code == 200 ? "Added: \(json(d)["title"] as? String ?? text)" : "Couldn't add that task", ok: code == 200)
        }
    }

    // Question -> Argo -> the pet's speech bubble. The pet shows "…" while Argo thinks (the poll keeps it in sync).
    func ask() {
        guard let q = textPrompt("Ask about your tasks and emails", placeholder: "what did Bob ask me about the beamtime?") else { return }
        pet.busy = true
        post("api/ask", ["q": q]) { d, code in
            self.pet.busy = false
            self.say(q, code == 200 ? json(d)["answer"] as? String ?? "" : "Argo is unavailable right now (down, or off VPN).", ok: code == 200)
        }
    }

    func menu() -> NSMenu {
        let m = NSMenu()
        m.autoenablesItems = false
        m.addItem(Item((window.isVisible ? "Hide " : "Show ") + window.title) { self.toggle(self.window) })
        m.addItem(.separator())
        m.addItem(Item("Quick Add Task…") {
            if let v = textPrompt("New task", info: "With Argo, plain language works.", placeholder: "review Bob's draft by Fri 3pm") { self.addTask(v) }
        })
        m.addItem(Item("Screenshot to Task…") { self.screenshotTask() })
        m.addItem(Item("Ask About My Tasks…") { self.ask() })
        m.addItem(.separator())
        let sound = Item("Sounds") { self.flip("petsMute") }
        sound.state = pref("petsMute") ? .off : .on
        m.addItem(sound)
        let tuck = Item("Tuck Into the Edge When the Mouse Is Away") { self.flip("petsTuck") }
        tuck.state = pref("petsTuck") ? .on : .off
        m.addItem(tuck)
        m.addItem(Item("Hide Erwin (⇧⌘P or the Dock icon brings him back)") { self.setPetsHidden(true) })
        return m
    }

    // Pet layout: the pet has a home spot (where you dropped it). Every tick moves it to where it belongs right now,
    // home or tucked, rather than remembering what it did last: a slide or fade cut short (by a bubble, a display
    // change, a drag) gets finished on the next tick instead of leaving the pet stuck.
    var tucked = false, home = NSPoint.zero, awaySince = Date(), settleUntil = Date.distantPast
    func pref(_ k: String) -> Bool { UserDefaults.standard.bool(forKey: k) }
    func flip(_ k: String) { UserDefaults.standard.set(!pref(k), forKey: k) }
    func setHome(_ o: NSPoint) {
        home = o
        UserDefaults.standard.set(NSStringFromPoint(o), forKey: "petHome.task")
    }
    var homeRect: NSRect { NSRect(origin: home, size: NSSize(width: petSize, height: petSize)) }
    func spot() -> NSRect {  // where the pet belongs now
        let r = homeRect, s = screenOf(r)
        return tucked ? tuckSpot(r, s.frame, s.visibleFrame, NSScreen.screens.filter { $0 != s }.map(\.frame)) : r
    }
    func tick() {  // tucking: out while the mouse is within 50 pt or a bubble shows, back in after 3 s away
        if !pref("petsTuck") || pref("petsHidden") || bubble.isVisible || panel.frame.insetBy(dx: -50, dy: -50).contains(NSEvent.mouseLocation) {
            awaySince = Date(); tucked = false
        } else if Date().timeIntervalSince(awaySince) > 3 { tucked = true }
        if NSEvent.pressedMouseButtons == 0 && Date() > settleUntil { settle() }  // not mid-drag, not mid-slide
    }
    func settle(animate: Bool = true) {  // move the pet if it isn't where spot() says
        let t = spot(), h = homeRect, v = screenOf(h).visibleFrame
        pet.tuckedInto = t == h ? nil : t.maxX <= v.minX + sliver + 1 ? .minX : t.minX >= v.maxX - sliver - 1 ? .maxX
                         : t.maxY <= v.minY + sliver + 1 ? .minY : .maxY  // tucked: drawn as a tab on the side still on screen
        guard hypot(t.minX - panel.frame.minX, t.minY - panel.frame.minY) > 0.5 || panel.alphaValue < 1 else { return }
        settleUntil = Date() + 0.35
        NSAnimationContext.runAnimationGroup { c in
            c.duration = animate ? 0.25 : 0
            if animate { panel.animator().setFrame(t, display: true); panel.animator().alphaValue = 1 }
            else { panel.setFrame(t, display: true); panel.alphaValue = 1 }
        }
    }
    @objc func screensChanged() {  // a display plugged in, unplugged or rearranged, or the Dock moved
        setHome(onScreen(homeRect))
        settle(animate: false)
    }
    func setPetsHidden(_ h: Bool) {
        UserDefaults.standard.set(h, forKey: "petsHidden")
        h ? panel.orderOut(nil) : panel.orderFrontRegardless()
        if h { bubble.orderOut(nil) }
    }

    func makePet(_ view: PetView) -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: petSize, height: petSize),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = false
        p.ignoresMouseEvents = false  // set explicitly, so clear pixels take clicks too: the whole square is the pet
        p.contentView = view
        view.registerForDraggedTypes(PetView.dragTypes)
        if !p.setFrameUsingName("pet"), let f = NSScreen.main?.visibleFrame {
            p.setFrameOrigin(NSPoint(x: f.maxX - petSize - 26, y: f.minY + 20))  // bottom-right corner
        }
        p.setContentSize(NSSize(width: petSize, height: petSize))  // saved frame may hold an old size
        view.keepOnScreen()
        p.orderFrontRegardless()
        return p
    }

    func toggle(_ w: NSWindow) {
        if w.isVisible && w.isKeyWindow && NSApp.isActive {
            w.orderOut(nil)
        } else {
            NSApp.activate(ignoringOtherApps: true)
            w.makeKeyAndOrderFront(nil)
        }
    }

    // the page's camera button
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if m.name == "shot" { screenshotTask() }
    }

    // server takes a moment to start; keep retrying
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { w.load(URLRequest(url: w === self.settingsWeb ? settingsURL : base)) }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if pref("petsHidden") { setPetsHidden(false) }
        window.makeKeyAndOrderFront(nil); return true
    }

    func applicationWillTerminate(_ n: Notification) {
        if server.isRunning { server.terminate() }
    }

    // Links in email bodies (target=_blank) open in the default browser, not inside the app.
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction,
                 windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let url = a.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // WKWebView has no built-in confirm()/prompt(); the page uses both.
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler done: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        done(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText d: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler done: @escaping (String?) -> Void) {
        done(textPrompt(p, text: d ?? ""))
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate

// Menus: Cmd+Q/W, plus Edit so Cmd+C/V/Z work in text fields.
let mainMenu = NSMenu()
let appMenu = NSMenu()
let settingsItem = NSMenuItem(title: "Settings…", action: #selector(App.openSettings), keyEquivalent: ",")
settingsItem.target = delegate
appMenu.addItem(settingsItem)
appMenu.addItem(.separator())
let petsItem = Item("Show or Hide Erwin") { delegate.setPetsHidden(!delegate.pref("petsHidden")) }
petsItem.keyEquivalent = "p"; petsItem.keyEquivalentModifierMask = [.command, .shift]
appMenu.addItem(petsItem)
appMenu.addItem(.separator())
appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
appMenu.addItem(withTitle: "Quit Survey Corps", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
let editMenu = NSMenu(title: "Edit")
for (title, sel, key) in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                          ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
    editMenu.addItem(withTitle: title, action: Selector(sel), keyEquivalent: key)
}
for m in [appMenu, editMenu] {
    let item = NSMenuItem(); item.submenu = m; mainMenu.addItem(item)
}
app.mainMenu = mainMenu
app.setActivationPolicy(.regular)
app.run()
