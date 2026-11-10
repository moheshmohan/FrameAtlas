import AVFoundation
import Foundation

@main
struct VideoGPS {
    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            FileHandle.standardError.write(Data("Provide one local video file.\n".utf8))
            exit(2)
        }
        let url = URL(fileURLWithPath: CommandLine.arguments[1])
        do {
            let asset = AVURLAsset(url: url)
            let metadata = try await asset.load(.metadata)
            let identifiers: Set<AVMetadataIdentifier> = [
                .quickTimeMetadataLocationISO6709, .quickTimeUserDataLocationISO6709,
            ]
            var values: Set<String> = []
            for item in metadata {
                guard let identifier = item.identifier, identifiers.contains(identifier) else { continue }
                if let value = try await item.load(.stringValue), value.utf8.count <= 128 {
                    values.insert(value)
                }
                if values.count > 16 {
                    FileHandle.standardError.write(Data("Too many QuickTime location values.\n".utf8))
                    exit(1)
                }
            }
            FileHandle.standardOutput.write(try JSONEncoder().encode(values.sorted()))
        } catch {
            FileHandle.standardError.write(Data("Could not read local QuickTime location metadata.\n".utf8))
            exit(1)
        }
    }
}
