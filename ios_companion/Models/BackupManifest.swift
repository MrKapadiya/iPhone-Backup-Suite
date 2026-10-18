import Foundation
import Photos

/// Represents the JSON manifest (`backed_up_manifest.json`) pushed by the desktop app
/// via `HouseArrestService` (`VendDocuments`) into `/Documents/backed_up_manifest.json`.
struct BackupManifest: Codable {
    let schemaVersion: Int
    let syncId: String
    let deviceUdid: String
    let syncedAt: String
    let totalVerifiedAssets: Int
    let totalVerifiedBytes: Int64
    let assets: [BackedUpAssetEntry]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case syncId = "sync_id"
        case deviceUdid = "device_udid"
        case syncedAt = "synced_at"
        case totalVerifiedAssets = "total_verified_assets"
        case totalVerifiedBytes = "total_verified_bytes"
        case assets
    }
}

struct BackedUpAssetEntry: Codable, Identifiable, Hashable {
    var id: String { "\(dcimFolder)/\(filename):\(fileSize)" }

    let filename: String
    let dcimFolder: String
    let relativeDcimPath: String
    let fileSize: Int64
    let createdTimestamp: Int64
    let sha256: String
    let isLivePhotoPaired: Bool
    let backedUpAtUtc: String

    enum CodingKeys: String, CodingKey {
        case filename
        case dcimFolder = "dcim_folder"
        case relativeDcimPath = "relative_dcim_path"
        case fileSize = "file_size"
        case createdTimestamp = "created_timestamp"
        case sha256
        case isLivePhotoPaired = "is_live_photo_paired"
        case backedUpAtUtc = "backed_up_at_utc"
    }
}

/// Represents a local iOS `PHAsset` that has been positively matched against the desktop manifest.
struct MatchedPhotoAsset: Identifiable {
    let id: String              // PHAsset.localIdentifier
    let phAsset: PHAsset
    let manifestEntry: BackedUpAssetEntry
}
