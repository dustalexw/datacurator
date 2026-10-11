import AppKit
import Foundation

// Launch the packaged app through LaunchServices, as Finder does. Offscreen Qt
// startup tests cannot catch a bundle that shows a window but cannot activate.
let bundle = URL(fileURLWithPath: CommandLine.arguments[1]).standardizedFileURL
let config = NSWorkspace.OpenConfiguration()
config.createsNewApplicationInstance = true
config.activates = true
config.environment = ["DATACURATOR_HOME": CommandLine.arguments[2]]
var finished = false
var result: Int32 = 1
NSWorkspace.shared.openApplication(at: bundle, configuration: config) { application, error in
    guard let application = application else {
        print("Launch failed: \(String(describing: error))")
        finished = true
        return
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
        let regular = application.activationPolicy == .regular
        let active = application.isActive
        print("Data Curator foreground activation: regular=\(regular), active=\(active)")
        result = regular && active ? 0 : 1
        application.terminate()
        finished = true
    }
}
let deadline = Date().addingTimeInterval(30)
while !finished && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}
exit(result)
