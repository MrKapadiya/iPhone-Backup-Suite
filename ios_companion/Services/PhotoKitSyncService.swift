import Foundation
import Photos
import SwiftUI

@MainActor
final class PhotoKitSyncService: ObservableObject {
    static let backedUpAlbumTitle = "Backed Up"
    static let manifestFileName = "backed_up_manifest.json"

    @Published var authorizationStatus: PHAuthorizationStatus = .notDetermined
    @Published var currentManifest: BackupManifest?
    @Published var matchedAssets: [MatchedPhotoAsset] = []
    @Published var isScanning: Bool = false
    @Published var statusMessage: String = "Connect to Desktop App and click 'Push Verified Manifest'."
    @Published var errorMessage: String?

    var reclaimableBytes: Int64 {
        matchedAssets.reduce(0) { $0 + $1.manifestEntry.fileSize }
    }

    init() {
        authorizationStatus = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    }

    func requestPhotoPermission() async {
        let status = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
        authorizationStatus = status
        if status == .authorized || status == .limited {
            await loadManifestFromDocumentsAndMatch()
        } else {
            errorMessage = "Full Photo Library permission is required to organize and delete backed-up photos."
        }
    }

    /// Reads `/Documents/backed_up_manifest.json` pushed over USB by `HouseArrestService`.
    func loadManifestFromDocumentsAndMatch() async {
        errorMessage = nil
        guard let docsDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
            return
        }
        let manifestURL = docsDir.appendingPathComponent(Self.manifestFileName)
        guard FileManager.default.fileExists(atPath: manifestURL.path) else {
            statusMessage = "No manifest found in /Documents yet. Push from the Desktop App via USB or Wi-Fi."
            return
        }

        do {
            let data = try Data(contentsOf: manifestURL)
            let manifest = try JSONDecoder().decode(BackupManifest.self, from: data)
            self.currentManifest = manifest
            await matchManifestAgainstPhotoLibrary(manifest: manifest)
        } catch {
            self.errorMessage = "Failed to decode manifest: \(error.localizedDescription)"
        }
    }

    /// Optional fallback: Pull `backed_up_manifest.json` from the Desktop LAN HTTP Server.
    func fetchManifestFromLanServer(urlString: String) async {
        errorMessage = nil
        guard let url = URL(string: urlString) else {
            errorMessage = "Invalid LAN URL."
            return
        }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let manifest = try JSONDecoder().decode(BackupManifest.self, from: data)

            if let docsDir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
                let dest = docsDir.appendingPathComponent(Self.manifestFileName)
                try? data.write(to: dest, options: .atomic)
            }
            self.currentManifest = manifest
            await matchManifestAgainstPhotoLibrary(manifest: manifest)
        } catch {
            errorMessage = "LAN Sync Failed: \(error.localizedDescription)"
        }
    }

    /// Matches manifest entries against local `PHAsset` objects using original filename + exact byte size
    /// (or creation timestamp tolerance) to prevent false matches on wrapped `IMG_XXXX` counters.
    func matchManifestAgainstPhotoLibrary(manifest: BackupManifest) async {
        isScanning = true
        statusMessage = "Matching \(manifest.assets.count) backed-up files against iPhone Photo Library..."

        var lookupByFilename: [String: [BackedUpAssetEntry]] = [:]
        for item in manifest.assets {
            let key = item.filename.uppercased()
            lookupByFilename[key, default: []].append(item)
        }

        let matched: [MatchedPhotoAsset] = await Task.detached(priority: .userInitiated) {
            var results: [MatchedPhotoAsset] = []
            let fetchOptions = PHFetchOptions()
            fetchOptions.includeHiddenAssets = false
            fetchOptions.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]

            let allAssets = PHAsset.fetchAssets(with: fetchOptions)
            allAssets.enumerateObjects { asset, _, _ in
                let resources = PHAssetResource.assetResources(for: asset)
                for res in resources {
                    let origName = res.originalFilename.uppercased()
                    guard let candidates = lookupByFilename[origName] else { continue }

                    let resFileSize = (res.value(forKey: "fileSize") as? CLong) ?? 0
                    let assetCreationTs = Int64(asset.creationDate?.timeIntervalSince1970 ?? 0)

                    if let exactMatch = candidates.first(where: { entry in
                        let sizeMatches = resFileSize > 0 && Int64(resFileSize) == entry.fileSize
                        let timeMatches = abs(assetCreationTs - entry.createdTimestamp) <= 3
                        return sizeMatches || timeMatches
                    }) {
                        results.append(
                            MatchedPhotoAsset(
                                id: asset.localIdentifier,
                                phAsset: asset,
                                manifestEntry: exactMatch
                            )
                        )
                        break
                    }
                }
            }
            return results
        }.value

        self.matchedAssets = matched
        self.isScanning = false
        let gbString = String(format: "%.2f GB", Double(reclaimableBytes) / 1_000_000_000.0)
        self.statusMessage = "Matched \(matched.count) backed-up photos/videos (\(gbString)) still on this iPhone."
    }

    /// Creates or fetches the 'Backed Up' album and adds all matched `PHAsset` objects to it.
    func addMatchedAssetsToBackedUpAlbum() async {
        guard !matchedAssets.isEmpty else { return }
        errorMessage = nil
        let phAssets = matchedAssets.map { $0.phAsset }

        do {
            let album = try await fetchOrCreateBackedUpAlbum()
            try await PHPhotoLibrary.shared().performChanges {
                guard let albumChangeReq = PHAssetCollectionChangeRequest(for: album) else { return }
                albumChangeReq.addAssets(phAssets as NSArray)
            }
            statusMessage = "✅ Added \(phAssets.count) verified photos to the '\(Self.backedUpAlbumTitle)' album!"
        } catch {
            errorMessage = "Could not update '\(Self.backedUpAlbumTitle)' album: \(error.localizedDescription)"
        }
    }

    /// Invokes Apple's native batch deletion sheet (`PHAssetChangeRequest.deleteAssets`)
    /// so all verified backed-up photos move to iOS 'Recently Deleted' in one tap.
    func deleteMatchedBackedUpAssets() async {
        guard !matchedAssets.isEmpty else { return }
        errorMessage = nil
        let phAssets = matchedAssets.map { $0.phAsset }

        do {
            try await PHPhotoLibrary.shared().performChanges {
                PHAssetChangeRequest.deleteAssets(phAssets as NSArray)
            }
            let deletedCount = phAssets.count
            matchedAssets.removeAll()
            statusMessage = "🎉 Deleted \(deletedCount) backed-up photos! They are in iOS 'Recently Deleted'."
        } catch {
            errorMessage = "Deletion cancelled or failed: \(error.localizedDescription)"
        }
    }

    private func fetchOrCreateBackedUpAlbum() async throws -> PHAssetCollection {
        let fetchOptions = PHFetchOptions()
        fetchOptions.predicate = NSPredicate(format: "title = %@", Self.backedUpAlbumTitle)
        let collections = PHAssetCollection.fetchAssetCollections(
            with: .album,
            subtype: .any,
            options: fetchOptions
        )
        if let existing = collections.firstObject {
            return existing
        }

        var placeholder: PHObjectPlaceholder?
        try await PHPhotoLibrary.shared().performChanges {
            let createReq = PHAssetCollectionChangeRequest.creationRequestForAssetCollection(
                withTitle: Self.backedUpAlbumTitle
            )
            placeholder = createReq.placeholderForCreatedAssetCollection
        }

        guard let localId = placeholder?.localIdentifier,
              let created = PHAssetCollection.fetchAssetCollections(
                  withLocalIdentifiers: [localId],
                  options: nil
              ).firstObject else {
            throw NSError(
                domain: "BackupCompanion",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Failed to create 'Backed Up' album."]
            )
        }
        return created
    }
}
