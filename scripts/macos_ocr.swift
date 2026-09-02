// macos_ocr.swift — on-device OCR via Apple's Vision framework.
//
// Usage:  macos_ocr <image-path>
// Prints the recognized text to stdout (reading order: top-to-bottom, then
// left-to-right), one observation per line. Exit 0 on success (even if no text
// was found — empty output), non-zero on a usage/decoding error.
//
// Used by the Agent Chat app (_macos_vision_ocr) to make image attachments
// (screenshots of dashboards, etc.) readable to text-only / container agents
// that cannot fetch the auth-walled binary or accept vision input. Free and
// fully local — no tokens, no network. Compiled once and cached by the app.
//
//   swiftc -O -o scripts/macos_ocr scripts/macos_ocr.swift

import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: macos_ocr <image-path>\n".utf8))
    exit(2)
}

let path = CommandLine.arguments[1]

guard let image = NSImage(contentsOfFile: path),
      let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let cgImage = bitmap.cgImage else {
    FileHandle.standardError.write(Data("could not decode image\n".utf8))
    exit(3)
}

// Collect (text, boundingBox) so we can emit in human reading order. Vision's
// boundingBox origin is bottom-left and normalized [0,1].
var observations: [(text: String, box: CGRect)] = []

let request = VNRecognizeTextRequest { (req, _) in
    guard let results = req.results as? [VNRecognizedTextObservation] else { return }
    for obs in results {
        if let best = obs.topCandidates(1).first {
            observations.append((best.string, obs.boundingBox))
        }
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write(Data("OCR failed: \(error)\n".utf8))
    exit(4)
}

// Sort top-to-bottom (y descending in Vision's bottom-left space), grouping
// observations on roughly the same line, then left-to-right within a line.
let rowTolerance: CGFloat = 0.012
observations.sort { a, b in
    if abs(a.box.midY - b.box.midY) > rowTolerance {
        return a.box.midY > b.box.midY
    }
    return a.box.minX < b.box.minX
}

let out = observations.map { $0.text }.joined(separator: "\n")
print(out)
