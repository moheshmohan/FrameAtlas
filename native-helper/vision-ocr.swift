// Original FrameAtlas helper. Apple Vision runs locally; no network or model fetch.
import Foundation
import Vision
import ImageIO
import CoreML

enum InputError: Error { case invalid(String) }
func run() throws {
    guard CommandLine.arguments.count == 2 else { throw InputError.invalid("Expected one local screenshot path") }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    let info = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
    guard info.isRegularFile == true, let size = info.fileSize, size > 0, size <= 6 * 1024 * 1024 else {
        throw InputError.invalid("Screenshot must be a regular image file no larger than 6 MiB")
    }
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0, width <= 8192, height <= 8192, width * height <= 16_000_000 else {
        throw InputError.invalid("Invalid or oversized screenshot")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    if #available(macOS 14.0, *) {
        for (stage, devices) in try request.supportedComputeStageDevices {
            guard let cpu = devices.first(where: { if case .cpu = $0 { return true }; return false }) else {
                throw InputError.invalid("A local CPU OCR device is unavailable")
            }
            request.setComputeDevice(cpu, for: stage)
        }
    } else {
        request.usesCPUOnly = true
    }
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.006
    try VNImageRequestHandler(url: url, options: [:]).perform([request])
    var lines: [[String: Any]] = []
    for observation in (request.results ?? []).prefix(1000) {
        guard let text = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        lines.append(["text": text.string, "confidence": text.confidence,
                      "x": box.minX, "y": box.minY, "width": box.width, "height": box.height])
    }
    let data = try JSONSerialization.data(withJSONObject: ["engine": "Apple Vision local OCR", "lines": lines])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}
do { try run() } catch {
    FileHandle.standardError.write(Data("Local OCR failed: \(error)\n".utf8))
    exit(1)
}
