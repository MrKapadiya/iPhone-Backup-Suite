import SwiftUI
import Photos

struct ContentView: View {
    @StateObject private var syncService = PhotoKitSyncService()
    @State private var lanUrlInput: String = "http://192.168.1.42:8765/backed_up_manifest.json"
    @State private var showLanSheet: Bool = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 18) {
                    // 1. Summary Card
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Image(systemName: "checkmark.shield.fill")
                                .font(.title2)
                                .foregroundStyle(.green)
                            Text("Verified PC/Mac Backup Sync")
                                .font(.headline)
                            Spacer()
                        }

                        Text(syncService.statusMessage)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)

                        if let manifest = syncService.currentManifest {
                            Divider()
                            HStack {
                                Label("\(manifest.totalVerifiedAssets) in Ledger", systemImage: "photo.stack")
                                Spacer()
                                Label(
                                    String(format: "%.2f GB Reclaimable", Double(syncService.reclaimableBytes) / 1e9),
                                    systemImage: "externaldrive.fill.badge.checkmark"
                                )
                                .foregroundStyle(.orange)
                            }
                            .font(.caption.bold())
                        }
                    }
                    .padding()
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 14))

                    if let err = syncService.errorMessage {
                        Text("⚠️ \(err)")
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .padding()
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.red.opacity(0.12))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                    }

                    // 2. Action Buttons
                    VStack(spacing: 12) {
                        Button {
                            Task { await syncService.loadManifestFromDocumentsAndMatch() }
                        } label: {
                            Label("Scan USB Manifest & Match Photos", systemImage: "arrow.triangle.2.circlepath")
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 6)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(syncService.isScanning)

                        Button {
                            Task { await syncService.addMatchedAssetsToBackedUpAlbum() }
                        } label: {
                            Label(
                                "Add \(syncService.matchedAssets.count) Verified Photos to 'Backed Up' Album",
                                systemImage: "folder.badge.plus"
                            )
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.bordered)
                        .disabled(syncService.matchedAssets.isEmpty || syncService.isScanning)

                        Button(role: .destructive) {
                            Task { await syncService.deleteMatchedBackedUpAssets() }
                        } label: {
                            Label(
                                String(
                                    format: "Delete %d Backed-Up Photos (Free %.2f GB)",
                                    syncService.matchedAssets.count,
                                    Double(syncService.reclaimableBytes) / 1e9
                                ),
                                systemImage: "trash.fill"
                            )
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.red)
                        .disabled(syncService.matchedAssets.isEmpty || syncService.isScanning)
                    }

                    // 3. Matched Asset List Preview
                    if !syncService.matchedAssets.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Verified Backed-Up Files Still on iPhone (\(syncService.matchedAssets.count))")
                                .font(.subheadline.bold())

                            ForEach(syncService.matchedAssets.prefix(50)) { item in
                                HStack {
                                    Image(systemName: item.manifestEntry.isLivePhotoPaired ? "livephoto" : "photo")
                                        .foregroundStyle(.blue)
                                    VStack(alignment: .leading) {
                                        Text(item.manifestEntry.filename)
                                            .font(.footnote.bold())
                                        Text("\(item.manifestEntry.dcimFolder) • SHA-256: \(item.manifestEntry.sha256.prefix(12))…")
                                            .font(.caption2)
                                            .foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Text(String(format: "%.1f MB", Double(item.manifestEntry.fileSize) / 1_048_576.0))
                                        .font(.caption.monospacedDigit())
                                }
                                .padding(.vertical, 4)
                                Divider()
                            }
                        }
                        .padding()
                        .background(Color(.secondarySystemBackground))
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                    }
                }
                .padding()
            }
            .navigationTitle("Backup Companion")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showLanSheet = true
                    } label: {
                        Image(systemName: "wifi")
                    }
                }
            }
            .task {
                await syncService.requestPhotoPermission()
            }
            .sheet(isPresented: $showLanSheet) {
                NavigationStack {
                    Form {
                        Section("Desktop Wi-Fi Manifest Server URL") {
                            TextField("http://192.168.1.42:8765/backed_up_manifest.json", text: $lanUrlInput)
                                .keyboardType(.URL)
                                .autocorrectionDisabled()
                                .textInputAutocapitalization(.never)
                        }
                        Section {
                            Button("Fetch Manifest Over Wi-Fi") {
                                showLanSheet = false
                                Task { await syncService.fetchManifestFromLanServer(urlString: lanUrlInput) }
                            }
                        }
                    }
                    .navigationTitle("Wi-Fi Sync Fallback")
                }
                .presentationDetents([.medium])
            }
        }
    }
}
