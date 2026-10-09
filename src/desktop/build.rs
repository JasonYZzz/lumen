fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "bootstrap",
            "choose_project",
            "reply",
            "cancel_startup",
            "forget_credential",
        ]),
    ))
    .expect("desktop build configuration");
}
