import AppKit
import ApplicationServices
import Foundation

private let bundleIdentifier = "ai.perplexity.macv3"
private let applicationURL = URL(fileURLWithPath: "/Applications/Perplexity.app")
private let arguments = Array(CommandLine.arguments.dropFirst())
private let resultFilePath: String? = {
    guard let index = arguments.firstIndex(of: "--result-file"), index + 1 < arguments.count else {
        return nil
    }
    return arguments[index + 1]
}()

private struct DriverError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

private struct AXNode {
    let element: AXUIElement
    let role: String
    let title: String
    let value: String
    let description: String
    let help: String
    let identifier: String
    let enabled: Bool
    let selected: Bool
    let valueSettable: Bool

    var searchableText: String {
        [title, value, description, help, identifier]
            .filter { !$0.isEmpty }
            .joined(separator: "\n")
    }
}

private func jsonLine(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object),
          let line = String(data: data, encoding: .utf8) else {
        print("{\"status\":\"error\",\"error\":\"Could not encode driver result.\"}")
        return
    }
    if let path = resultFilePath {
        do {
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600], ofItemAtPath: path
            )
        } catch {
            fputs("Could not write driver result.\n", stderr)
        }
    } else {
        print(line)
    }
}

private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else {
        return nil
    }
    return value
}

private func stringAttribute(_ element: AXUIElement, _ name: String) -> String {
    guard let value = attribute(element, name) else { return "" }
    if let string = value as? String { return string }
    if let attributed = value as? NSAttributedString { return attributed.string }
    return ""
}

private func boolAttribute(_ element: AXUIElement, _ name: String, default fallback: Bool) -> Bool {
    guard let value = attribute(element, name) else { return fallback }
    if let number = value as? NSNumber { return number.boolValue }
    return fallback
}

private func childElements(_ element: AXUIElement) -> [AXUIElement] {
    (attribute(element, kAXChildrenAttribute) as? [AXUIElement]) ?? []
}

private func allNodes(_ root: AXUIElement, limit: Int = 4_000) -> [AXNode] {
    var result: [AXNode] = []
    var queue: [AXUIElement] = [root]
    var offset = 0
    while offset < queue.count && result.count < limit {
        let element = queue[offset]
        offset += 1
        var settable = DarwinBoolean(false)
        let settableResult = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
        result.append(AXNode(
            element: element,
            role: stringAttribute(element, kAXRoleAttribute),
            title: stringAttribute(element, kAXTitleAttribute),
            value: stringAttribute(element, kAXValueAttribute),
            description: stringAttribute(element, kAXDescriptionAttribute),
            help: stringAttribute(element, kAXHelpAttribute),
            identifier: stringAttribute(element, kAXIdentifierAttribute),
            enabled: boolAttribute(element, kAXEnabledAttribute, default: true),
            selected: boolAttribute(element, kAXSelectedAttribute, default: false),
            valueSettable: settableResult == .success && settable.boolValue
        ))
        queue.append(contentsOf: childElements(element))
    }
    return result
}

private func waitUntil(timeout: TimeInterval, interval: TimeInterval = 0.25, _ predicate: () -> Bool) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
        if predicate() { return true }
        RunLoop.current.run(until: Date().addingTimeInterval(interval))
    } while Date() < deadline
    return false
}

private func runningApplication(launchIfNeeded: Bool) throws -> NSRunningApplication {
    if let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).first {
        return running
    }
    guard launchIfNeeded, FileManager.default.fileExists(atPath: applicationURL.path) else {
        throw DriverError("Perplexity is not installed at /Applications/Perplexity.app.")
    }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.activates = false
    let semaphore = DispatchSemaphore(value: 0)
    var launched: NSRunningApplication?
    var launchError: Error?
    NSWorkspace.shared.openApplication(at: applicationURL, configuration: configuration) { app, error in
        launched = app
        launchError = error
        semaphore.signal()
    }
    _ = semaphore.wait(timeout: .now() + 10)
    if let error = launchError { throw error }
    guard let app = launched ?? NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).first else {
        throw DriverError("Perplexity did not start.")
    }
    return app
}

private func appRoot(_ app: NSRunningApplication) -> AXUIElement {
    AXUIElementCreateApplication(app.processIdentifier)
}

private func press(_ node: AXNode) -> Bool {
    AXUIElementPerformAction(node.element, kAXPressAction as CFString) == .success
}

private func activateRow(_ node: AXNode) -> Bool {
    let selected = AXUIElementSetAttributeValue(
        node.element, kAXSelectedAttribute as CFString, kCFBooleanTrue
    ) == .success
    _ = AXUIElementSetAttributeValue(
        node.element, kAXFocusedAttribute as CFString, kCFBooleanTrue
    )
    let pressed = press(node)
    return selected || pressed
}

private func nodeStrings(_ node: AXNode) -> [String] {
    [node.title, node.value, node.description, node.help, node.identifier]
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
}

private func nodeMatches(_ node: AXNode, labels: [String]) -> Bool {
    let wanted = Set(labels)
    return nodeStrings(node).contains { wanted.contains($0) }
}

private func matchingNode(_ root: AXUIElement, labels: [String]) -> AXNode? {
    allNodes(root).first { $0.enabled && nodeMatches($0, labels: labels) }
}

@discardableResult
private func pressControl(_ root: AXUIElement, labels: [String]) -> Bool {
    for node in allNodes(root) where node.enabled && nodeMatches(node, labels: labels) {
        if press(node) { return true }
    }
    return false
}

private struct AXSurface {
    let application: NSRunningApplication
    let root: AXUIElement
}

private let openPanelBundleIdentifier = "com.apple.appkit.xpc.openAndSavePanelService"
private let viewBridgeHostKey =
    "ViewBridgeMostRecentlyProxiedKeyboardEventsForUltimateHostApp"
private let folderChooserMenuLabels = [
    "Choose Folder",
    "Choose a different folder",
    "Add Folder",
    "Add another folder",
    "Choose another folder",
]
private let folderChooserAcceptLabels = ["Open", "Choose", "Select"]

private func isFolderChooser(_ nodes: [AXNode]) -> Bool {
    // Perplexity's macOS folder picker is a native open panel. Bind to that exact
    // panel identifier plus the standard button identifiers so an unrelated
    // sheet cannot receive the workspace path.
    let panel = nodes.contains { $0.identifier == "open-panel" }
    let cancel = nodes.contains {
        $0.role == (kAXButtonRole as String) &&
            $0.identifier == "CancelButton"
    }
    let select = nodes.contains {
        $0.role == (kAXButtonRole as String) &&
            $0.identifier == "OKButton" &&
            nodeMatches($0, labels: folderChooserAcceptLabels)
    }
    return panel && cancel && select
}

private func folderChooserRoot(_ perplexityRoot: AXUIElement) -> AXUIElement? {
    let nodes = allNodes(perplexityRoot)
    // Prefer the identified panel itself. Its role may vary between native
    // macOS releases, so the stable identifier is more useful than AXSheet.
    for container in nodes where container.identifier == "open-panel" &&
            isFolderChooser(allNodes(container.element)) {
        return container.element
    }
    return nil
}

private func lsAppInfoValue(_ key: String, appSpecifier: String) -> String? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/lsappinfo")
    process.arguments = ["info", "-only", key, "-app", appSpecifier]
    let output = Pipe()
    process.standardOutput = output
    process.standardError = Pipe()
    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        return nil
    }
    guard process.terminationStatus == 0,
          let text = String(
              data: output.fileHandleForReading.readDataToEndOfFile(),
              encoding: .utf8
          ),
          let separator = text.firstIndex(of: "=") else {
        return nil
    }
    var value = text[text.index(after: separator)...]
        .trimmingCharacters(in: .whitespacesAndNewlines)
    if value.count >= 2, value.first == "\"", value.last == "\"" {
        value.removeFirst()
        value.removeLast()
    }
    return value.isEmpty || value == "[ NULL ]" ? nil : value
}

private func panelBelongsToPerplexity(
    _ panel: NSRunningApplication,
    perplexityApp: NSRunningApplication
) -> Bool {
    guard panel.bundleIdentifier == openPanelBundleIdentifier,
          let hostASN = lsAppInfoValue(
              viewBridgeHostKey,
              appSpecifier: String(panel.processIdentifier)
          ),
          let hostBundle = lsAppInfoValue("bundleid", appSpecifier: hostASN),
          let hostPIDText = lsAppInfoValue("pid", appSpecifier: hostASN),
          let hostPID = pid_t(hostPIDText) else {
        return false
    }
    return hostBundle == bundleIdentifier &&
        hostPID == perplexityApp.processIdentifier
}

private func folderChooserSurface(
    perplexityApp: NSRunningApplication
) -> AXSurface? {
    let ownedPanels = NSRunningApplication.runningApplications(
        withBundleIdentifier: openPanelBundleIdentifier
    ).filter { panelBelongsToPerplexity($0, perplexityApp: perplexityApp) }

    // Current Perplexity builds expose the native open panel as a proxied sheet
    // inside Perplexity's own accessibility tree.  Prefer that exact app-bound
    // surface when present: it is already rooted at the verified Perplexity PID
    // and still has to pass the strict open-panel/button signature below. The
    // verified panel process is retained as ownership proof, while keyboard
    // focus remains with the host Perplexity application.
    let perplexityRoot = appRoot(perplexityApp)
    if let chooserRoot = folderChooserRoot(perplexityRoot) {
        guard !ownedPanels.isEmpty else { return nil }
        return AXSurface(application: perplexityApp, root: chooserRoot)
    }

    // Since macOS 10.15, native open panels live in an AppKit XPC process. Do
    // not scan arbitrary standalone panels: require LaunchServices/ViewBridge
    // to resolve the panel's ultimate host back to this exact Perplexity
    // process, then require the strict open-panel accessibility signature.
    for panel in ownedPanels {
        let panelRoot = appRoot(panel)
        if let chooserRoot = folderChooserRoot(panelRoot) {
            return AXSurface(application: panel, root: chooserRoot)
        }
    }
    return nil
}

private func waitForFolderChooser(
    perplexityApp: NSRunningApplication,
    timeout: TimeInterval
) -> AXSurface? {
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
        if let surface = folderChooserSurface(perplexityApp: perplexityApp) {
            return surface
        }
        RunLoop.current.run(until: Date().addingTimeInterval(0.2))
    } while Date() < deadline
    return nil
}

private func postKey(
    virtualKey: CGKeyCode,
    flags: CGEventFlags,
    to application: NSRunningApplication
) throws {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: virtualKey, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: virtualKey, keyDown: false) else {
        throw DriverError("Could not create a keyboard event for Perplexity's folder chooser.")
    }
    down.flags = flags
    up.flags = flags
    // AppKit's out-of-process open panel ignores PID-targeted key events even
    // though its accessibility tree is proxied through Perplexity. Keep the
    // verified host frontmost and post through the HID event tap, matching a
    // physical Cmd-Shift-G/Return delivered to that native sheet.
    _ = application.activate(options: [.activateIgnoringOtherApps])
    RunLoop.current.run(until: Date().addingTimeInterval(0.15))
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
}

private func editablePathField(_ root: AXUIElement) -> AXNode? {
    allNodes(root).first {
        $0.role == (kAXTextFieldRole as String) && $0.valueSettable && $0.enabled &&
            $0.identifier == "PathTextField" && hasAncestorIdentifier(
                $0.element, identifier: "GoToWindow"
            )
    }
}

private func hasAncestorIdentifier(
    _ element: AXUIElement,
    identifier: String,
    limit: Int = 12
) -> Bool {
    var current = element
    for _ in 0..<limit {
        guard let rawParent = attribute(current, kAXParentAttribute),
              CFGetTypeID(rawParent) == AXUIElementGetTypeID() else {
            return false
        }
        let parent = unsafeBitCast(rawParent, to: AXUIElement.self)
        if stringAttribute(parent, kAXIdentifierAttribute) == identifier {
            return true
        }
        current = parent
    }
    return false
}

@discardableResult
private func pressFolderChooserSelect(_ root: AXUIElement) -> Bool {
    guard let button = allNodes(root).first(where: {
        $0.role == (kAXButtonRole as String) && $0.enabled &&
            $0.identifier == "OKButton" &&
            nodeMatches($0, labels: folderChooserAcceptLabels)
    }) else { return false }
    return press(button)
}

@discardableResult
private func cancelFolderChooser(_ root: AXUIElement) -> Bool {
    guard let button = allNodes(root).first(where: {
        $0.role == (kAXButtonRole as String) && $0.enabled &&
            $0.identifier == "CancelButton"
    }) else { return false }
    return press(button)
}

private func normalizedFilePath(_ value: CFTypeRef?) -> String? {
    guard let value else { return nil }
    let raw: String
    if let url = value as? URL {
        guard url.isFileURL else { return nil }
        raw = url.path
    } else if let string = value as? String {
        if string.hasPrefix("file://"), let url = URL(string: string), url.isFileURL {
            raw = url.path
        } else if string.hasPrefix("/") {
            raw = string
        } else {
            return nil
        }
    } else {
        return nil
    }
    return URL(fileURLWithPath: raw).standardizedFileURL.resolvingSymlinksInPath().path
}

private func chooserShowsExactFolder(_ root: AXUIElement, folder: String) -> Bool {
    let expected = URL(fileURLWithPath: folder)
        .standardizedFileURL.resolvingSymlinksInPath().path
    for node in allNodes(root) where
            node.selected && node.role == (kAXTextFieldRole as String) {
        if normalizedFilePath(
            attribute(node.element, kAXURLAttribute as String)
        ) == expected {
            return true
        }
    }
    return false
}

private func workspaceFolderChipCount(_ root: AXUIElement, folder: String) -> Int {
    let folderName = URL(fileURLWithPath: folder).lastPathComponent
    guard !folderName.isEmpty else { return 0 }
    return allNodes(root).filter {
        $0.role == (kAXButtonRole as String) && $0.enabled &&
            nodeMatches($0, labels: [folderName])
    }.count
}

@discardableResult
private func clearUnsentComposer(_ root: AXUIElement) -> Bool {
    guard let composer = findComposer(allNodes(root)) else { return false }
    let cleared = AXUIElementSetAttributeValue(
        composer.element, kAXValueAttribute as CFString, "" as CFTypeRef
    ) == .success
    return cleared && stringAttribute(composer.element, kAXValueAttribute).isEmpty
}

@discardableResult
private func removeWorkspaceFolder(_ folder: String, root: AXUIElement) -> Bool {
    let initialCount = workspaceFolderChipCount(root, folder: folder)
    if initialCount == 0 { return true }

    let folderName = URL(fileURLWithPath: folder).lastPathComponent
    guard pressControl(root, labels: [folderName]),
          waitUntil(timeout: 2, interval: 0.15, {
              matchingNode(root, labels: ["Remove Workspace"]) != nil
          }),
          pressControl(root, labels: ["Remove Workspace"]) else {
        return false
    }
    return waitUntil(timeout: 3, interval: 0.15) {
        workspaceFolderChipCount(root, folder: folder) < initialCount
    }
}

private func selectWorkspaceFolder(
    _ folder: String,
    app: NSRunningApplication,
    root: AXUIElement
) throws {
    _ = app.activate(options: [])
    RunLoop.current.run(until: Date().addingTimeInterval(0.25))

    guard pressControl(root, labels: ["More Options"]) else {
        throw DriverError("Perplexity's More Options control is unavailable; the workspace was not attached.")
    }
    guard waitUntil(timeout: 3, interval: 0.15, {
        matchingNode(root, labels: ["Folders"]) != nil
    }), pressControl(root, labels: ["Folders"]) else {
        throw DriverError("Perplexity's Folders control is unavailable; the workspace was not attached.")
    }
    var chooser = waitForFolderChooser(
        perplexityApp: app, timeout: 1.0
    )
    if chooser == nil {
        guard waitUntil(timeout: 3, interval: 0.15, {
            matchingNode(root, labels: folderChooserMenuLabels) != nil
        }), pressControl(root, labels: folderChooserMenuLabels) else {
            throw DriverError("Perplexity did not offer its native folder chooser; no prompt was sent.")
        }
        chooser = waitForFolderChooser(
            perplexityApp: app, timeout: 5.0
        )
    }
    guard let chooser else {
        throw DriverError("Perplexity's native folder chooser did not open; no prompt was sent.")
    }
    var selectionConfirmed = false
    defer {
        if !selectionConfirmed {
            if let activeChooser = folderChooserSurface(perplexityApp: app) {
                _ = cancelFolderChooser(activeChooser.root)
                _ = waitUntil(timeout: 2, interval: 0.15) {
                    folderChooserSurface(perplexityApp: app) == nil
                }
            }
            // Perplexity keeps one global unsent draft, and New Task is a no-op
            // while that draft is visible. Remove the exact request-scoped
            // grant in place instead of relying on a new composer to discard it.
            _ = removeWorkspaceFolder(folder, root: root)
            _ = clearUnsentComposer(root)
        }
    }

    _ = chooser.application.activate(options: [])
    try postKey(
        virtualKey: 5, // G
        flags: [.maskCommand, .maskShift],
        to: chooser.application
    )
    var locationField: AXNode?
    guard waitUntil(timeout: 3, interval: 0.15, {
        // Go to Folder temporarily replaces the open-panel sheet with a
        // GoToWindow sheet at the panel-service root, so reacquire that root.
        locationField = editablePathField(appRoot(chooser.application))
        return locationField != nil
    }), let locationField else {
        throw DriverError("Perplexity's folder chooser did not expose Go to Folder; no prompt was sent.")
    }
    guard AXUIElementSetAttributeValue(
        locationField.element, kAXValueAttribute as CFString, folder as CFTypeRef
    ) == .success,
    stringAttribute(locationField.element, kAXValueAttribute) == folder else {
        throw DriverError("Could not place the exact workspace path in Perplexity's folder chooser.")
    }
    _ = AXUIElementSetAttributeValue(
        locationField.element, kAXFocusedAttribute as CFString, kCFBooleanTrue
    )
    try postKey(virtualKey: 36, flags: [], to: chooser.application) // Return

    // The native GoToWindow has no Go button: Return accepts PathTextField.  Do
    // not settle for the sheet merely closing; require the open panel to expose
    // the exact selected folder URL before pressing Select.
    var navigatedChooser: AXSurface?
    let navigated = waitUntil(timeout: 5, interval: 0.2) {
        let pathFieldClosed = allNodes(appRoot(chooser.application)).allSatisfy {
            $0.identifier != "PathTextField"
        }
        navigatedChooser = folderChooserSurface(perplexityApp: app)
        return pathFieldClosed && navigatedChooser.map {
            chooserShowsExactFolder($0.root, folder: folder)
        } == true
    }
    guard navigated, let navigatedChooser else {
        throw DriverError("Perplexity's folder chooser could not confirm the exact workspace; no prompt was sent.")
    }
    guard pressFolderChooserSelect(navigatedChooser.root) else {
        throw DriverError("Perplexity's folder chooser could not select the workspace; no prompt was sent.")
    }

    let chooserClosed = waitUntil(timeout: 8, interval: 0.2) {
        folderChooserSurface(perplexityApp: app) == nil
    }
    let chipAttached = chooserClosed && waitUntil(timeout: 5, interval: 0.2) {
        // Perplexity deduplicates an already-selected folder, so an exact
        // chooser confirmation may leave one existing chip rather than add a
        // second.  The native picker proof above establishes the exact path;
        // here we only require that its folder chip is present afterward.
        workspaceFolderChipCount(root, folder: folder) > 0
    }
    let composerReady = chipAttached && waitUntil(timeout: 5, interval: 0.2) {
        findComposer(allNodes(root)) != nil
    }
    guard composerReady else {
        throw DriverError(
            "Perplexity did not expose the expected workspace-folder chip; no prompt was sent."
        )
    }
    selectionConfirmed = true
}

private func responseTexts(_ nodes: [AXNode]) -> [String] {
    nodes.compactMap { node in
        guard node.role == (kAXTextAreaRole as String), !node.valueSettable else { return nil }
        let text = node.value.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}

private func currentThreadTitle(_ nodes: [AXNode]) -> String {
    let excluded = Set(["Recent", "Projects", "Artifacts", "Customize", "History", "Thinking"])
    for node in nodes where node.role == (kAXStaticTextRole as String) {
        let text = (node.value.isEmpty ? node.title : node.value)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty && !excluded.contains(text) && text.count <= 180 {
            var positionValue: CFTypeRef?
            if AXUIElementCopyAttributeValue(node.element, kAXPositionAttribute as CFString, &positionValue) == .success,
               let raw = positionValue, CFGetTypeID(raw) == AXValueGetTypeID() {
                var point = CGPoint.zero
                if AXValueGetValue(raw as! AXValue, .cgPoint, &point), point.x > 300, point.y < 140 {
                    return text
                }
            }
        }
    }
    return ""
}

private func rowLabels(_ row: AXNode) -> [String] {
    var seen = Set<String>()
    var labels: [String] = []
    for node in allNodes(row.element, limit: 80) {
        for label in nodeStrings(node) where seen.insert(label).inserted {
            labels.append(label)
        }
    }
    return labels
}

private func openThreadIfVisible(_ title: String, root: AXUIElement) -> Bool {
    guard !title.isEmpty else { return false }
    let exact = allNodes(root).first { node in
        node.role == (kAXRowRole as String) &&
            rowLabels(node).contains(title)
    }
    guard let row = exact, activateRow(row) else { return false }
    return waitUntil(timeout: 3) { currentThreadTitle(allNodes(root)) == title }
}

@discardableResult
private func pressNewTask(_ root: AXUIElement) -> Bool {
    guard let button = allNodes(root).first(where: {
        $0.role == (kAXButtonRole as String) && $0.enabled &&
            [$0.title, $0.value, $0.description, $0.help].contains(where: {
                $0.trimmingCharacters(in: .whitespacesAndNewlines) == "New Task"
            })
    }) else { return false }
    return press(button)
}

private func openAnyVisibleThread(_ root: AXUIElement) -> Bool {
    let excluded = Set(["Projects", "Recent", "Artifacts", "Customize", "History"])
    for row in allNodes(root) where row.role == (kAXRowRole as String) && row.enabled {
        let labels = rowLabels(row)
        guard !labels.contains("folder") else { continue }
        for label in labels where !label.contains("\n") && !excluded.contains(label) {
            if openThreadIfVisible(label, root: root) {
                return true
            }
        }
    }
    return false
}

private func openNewTask(app: NSRunningApplication, root: AXUIElement) throws {
    _ = app.activate(options: [])
    RunLoop.current.run(until: Date().addingTimeInterval(0.4))
    guard pressNewTask(root) else {
        throw DriverError("Perplexity's New Task button is unavailable.")
    }
    let ready = waitUntil(timeout: 5) {
        allNodes(root).contains(where: {
            $0.role == (kAXTextAreaRole as String) && $0.valueSettable &&
                $0.help.contains("Start a task")
        })
    }
    guard ready else {
        throw DriverError("Perplexity did not open a fresh task composer.")
    }
    // Current Perplexity builds preserve one unsent draft across New Task
    // clicks. Clearing its text is therefore the reliable fresh-task boundary;
    // request-scoped workspace chips are independently confirmed or removed.
    guard clearUnsentComposer(root) else {
        throw DriverError("Perplexity's fresh task composer could not be cleared.")
    }
}

private func findComposer(_ nodes: [AXNode]) -> AXNode? {
    let candidates = nodes.filter {
        $0.role == (kAXTextAreaRole as String) && $0.valueSettable && $0.enabled
    }
    return candidates.first(where: { $0.help.contains("Start a task") }) ?? candidates.first
}

private func sendPrompt(_ prompt: String, app: NSRunningApplication, root: AXUIElement) throws {
    guard let composer = findComposer(allNodes(root)) else {
        throw DriverError("Perplexity's task composer is unavailable.")
    }
    guard AXUIElementSetAttributeValue(composer.element, kAXValueAttribute as CFString, prompt as CFTypeRef) == .success else {
        throw DriverError("Could not place the request in Perplexity's composer.")
    }
    _ = AXUIElementSetAttributeValue(composer.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    RunLoop.current.run(until: Date().addingTimeInterval(0.35))

    if let send = allNodes(root).first(where: {
        $0.role == (kAXButtonRole as String) && $0.enabled &&
            ($0.help.localizedCaseInsensitiveContains("send") ||
             $0.description.localizedCaseInsensitiveContains("arrow-up"))
    }), press(send) {
        return
    }

    _ = app.activate(options: [])
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: 36, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 36, keyDown: false) else {
        throw DriverError("Could not create the Return key event for Perplexity.")
    }
    down.postToPid(app.processIdentifier)
    up.postToPid(app.processIdentifier)
}

private func runPrompt(
    prompt: String,
    freshPrompt: String,
    priorThreadTitle: String,
    workspaceFolder: String,
    timeout: TimeInterval
) throws -> [String: Any] {
    guard AXIsProcessTrusted() else {
        throw DriverError("Accessibility permission is required for Agent Chat Perplexity Driver.")
    }
    let app = try runningApplication(launchIfNeeded: true)
    let root = appRoot(app)
    let initialNodes = allNodes(root)
    // Folder selection is scoped to the current request and is available only
    // from a fresh task. The bridge also withholds the prior title, but enforce
    // that boundary here so direct/helper callers cannot accidentally resume a
    // task carrying a stale or absent folder grant.
    let openedExisting = workspaceFolder.isEmpty && !priorThreadTitle.isEmpty &&
        (currentThreadTitle(initialNodes) == priorThreadTitle || openThreadIfVisible(priorThreadTitle, root: root))
    if !openedExisting { try openNewTask(app: app, root: root) }
    var workspaceSelected = false
    var promptSent = false
    defer {
        if workspaceSelected && !promptSent {
            _ = removeWorkspaceFolder(workspaceFolder, root: root)
            _ = clearUnsentComposer(root)
        }
    }
    if !workspaceFolder.isEmpty {
        try selectWorkspaceFolder(workspaceFolder, app: app, root: root)
        workspaceSelected = true
    }

    let submittedPrompt = openedExisting ? prompt : freshPrompt
    let baseline = responseTexts(allNodes(root))
    try sendPrompt(submittedPrompt, app: app, root: root)
    promptSent = true

    let deadline = Date().addingTimeInterval(timeout)
    var lastCandidate = ""
    var stablePolls = 0
    var finalTitle = ""
    while Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.5))
        let nodes = allNodes(root)
        let responses = responseTexts(nodes)
        let stopActive = nodes.contains(where: {
            $0.role == (kAXButtonRole as String) && $0.enabled &&
                ($0.help.localizedCaseInsensitiveContains("stop") ||
                 $0.description.localizedCaseInsensitiveContains("player-stop"))
        })
        let newResponses = responses.count > baseline.count
            ? Array(responses.dropFirst(baseline.count))
            : ((responses.last != baseline.last) ? Array(responses.suffix(1)) : [])
        let candidate = newResponses.joined(separator: "\n\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if !candidate.isEmpty {
            if candidate == lastCandidate { stablePolls += 1 } else { stablePolls = 0 }
            lastCandidate = candidate
        }
        finalTitle = currentThreadTitle(nodes)
        if !stopActive && !lastCandidate.isEmpty && stablePolls >= 1 {
            return [
                "status": "ok",
                "response": lastCandidate,
                "thread_title": finalTitle,
                "continued": openedExisting,
                "workspace_selected": workspaceSelected,
            ]
        }
    }
    if !lastCandidate.isEmpty {
        return [
            "status": "timeout-partial",
            "response": lastCandidate,
            "thread_title": finalTitle,
            "continued": openedExisting,
            "workspace_selected": workspaceSelected,
        ]
    }
    throw DriverError("Perplexity did not return readable response text before the timeout.")
}

private func canonicalWorkspaceFolder(_ rawFolder: String) throws -> String {
    guard !rawFolder.isEmpty, rawFolder.hasPrefix("/"),
          !rawFolder.unicodeScalars.contains(where: {
              $0.value < 32 || $0.value == 127
          }) else {
        throw DriverError("The workspace folder must be an existing absolute directory.")
    }
    let canonical = URL(fileURLWithPath: rawFolder)
        .standardizedFileURL.resolvingSymlinksInPath().path
    var isDirectory: ObjCBool = false
    guard FileManager.default.fileExists(atPath: canonical, isDirectory: &isDirectory),
          isDirectory.boolValue else {
        throw DriverError("The workspace folder must be an existing absolute directory.")
    }
    return canonical
}

private func probeWorkspaceFolder(_ folder: String) throws -> [String: Any] {
    guard AXIsProcessTrusted() else {
        throw DriverError("Accessibility permission is required for Agent Chat Perplexity Driver.")
    }
    let app = try runningApplication(launchIfNeeded: true)
    let root = appRoot(app)
    let priorTitle = currentThreadTitle(allNodes(root))
    try openNewTask(app: app, root: root)
    defer {
        // A probe never sends a prompt. Remove its exact folder grant and clear
        // the retained global draft before restoring the prior visible task.
        _ = removeWorkspaceFolder(folder, root: root)
        _ = clearUnsentComposer(root)
        if !priorTitle.isEmpty {
            _ = openThreadIfVisible(priorTitle, root: root)
        }
    }
    try selectWorkspaceFolder(folder, app: app, root: root)
    return [
        "status": "ok",
        "workspace_selected": true,
        "prompt_sent": false,
    ]
}

private func optionValue(_ name: String, arguments: [String]) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}

if arguments.contains("--request-accessibility") {
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    let trusted = AXIsProcessTrustedWithOptions(options)
    jsonLine(["status": trusted ? "ok" : "accessibility-requested", "trusted": trusted])
    exit(trusted ? 0 : 2)
}

if arguments.contains("--health") {
    let installed = FileManager.default.fileExists(atPath: applicationURL.path)
    let running = !NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier).isEmpty
    let trusted = AXIsProcessTrusted()
    let status = !installed ? "app-missing" : (!trusted ? "accessibility-required" : "ok")
    jsonLine([
        "status": status,
        "installed": installed,
        "running": running,
        "accessibility_trusted": trusted,
        "bundle_id": bundleIdentifier,
        "workspace_folder_selection": true,
    ])
    exit(status == "ok" ? 0 : 2)
}

if arguments.contains("--workspace-folder-capability") {
    jsonLine([
        "status": "ok",
        "workspace_folder_selection": true,
    ])
    exit(0)
}

if let rawProbeFolder = optionValue("--workspace-folder-probe", arguments: arguments) {
    do {
        let folder = try canonicalWorkspaceFolder(rawProbeFolder)
        jsonLine(try probeWorkspaceFolder(folder))
        exit(0)
    } catch {
        jsonLine(["status": "error", "error": String(describing: error)])
        exit(1)
    }
}

do {
    guard let promptPath = optionValue("--prompt-file", arguments: arguments) else {
        throw DriverError("--prompt-file is required.")
    }
    let prompt = try String(contentsOfFile: promptPath, encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    if prompt.isEmpty { throw DriverError("The prompt is empty.") }
    let freshPrompt: String
    if let freshPath = optionValue("--fresh-prompt-file", arguments: arguments) {
        freshPrompt = try String(contentsOfFile: freshPath, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    } else {
        freshPrompt = prompt
    }
    if freshPrompt.isEmpty { throw DriverError("The fresh-task prompt is empty.") }
    let timeout = TimeInterval(optionValue("--timeout", arguments: arguments) ?? "900") ?? 900
    let title = optionValue("--thread-title", arguments: arguments) ?? ""
    let workspaceFolder = try optionValue("--workspace-folder", arguments: arguments)
        .map(canonicalWorkspaceFolder) ?? ""
    jsonLine(try runPrompt(
        prompt: prompt,
        freshPrompt: freshPrompt,
        priorThreadTitle: title,
        workspaceFolder: workspaceFolder,
        timeout: max(10, min(timeout, 3600))
    ))
} catch {
    jsonLine(["status": "error", "error": String(describing: error)])
    exit(1)
}
