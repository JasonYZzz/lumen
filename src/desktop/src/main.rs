#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    AppHandle, Emitter, Manager, RunEvent, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

const LIMIT: usize = 65_536;
const SERVICE: &str = "io.github.lumen.agent.credentials";
type Shared = Arc<Mutex<Desktop>>;

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Preferences {
    recent: Vec<PathBuf>,
    width: Option<f64>,
    height: Option<f64>,
    #[serde(default)]
    tool_dirs: Vec<PathBuf>,
}
struct Process {
    child: Arc<Mutex<Child>>,
    input: Option<ChildStdin>,
    port: Option<u16>,
    pending: Option<Value>,
    action: Option<&'static str>,
    shutdown: bool,
}
#[derive(Default)]
struct Desktop {
    process: Option<Process>,
    preferences: Preferences,
    quitting: bool,
    error: Option<String>,
}

fn local(url: &tauri::Url) -> bool {
    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http"
            && url.host_str() == Some("tauri.localhost")
            && url.port().is_none())
}
fn guard(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == "main" && local(&window.url().map_err(|_| "无法检查窗口来源")?) {
        Ok(())
    } else {
        Err("当前页面无权调用桌面启动接口".into())
    }
}
fn preferences(app: &AppHandle) -> Option<PathBuf> {
    app.path()
        .app_data_dir()
        .ok()
        .map(|p| p.join("desktop.json"))
}
fn save(app: &AppHandle, state: &Desktop) {
    let Some(path) = preferences(app) else { return };
    let Some(parent) = path.parent() else { return };
    if std::fs::create_dir_all(parent).is_err() {
        return;
    }
    let temporary = path.with_extension("json.tmp");
    if let Ok(bytes) = serde_json::to_vec(&state.preferences) {
        if std::fs::write(&temporary, bytes).is_ok() {
            let _ = std::fs::rename(temporary, path);
        }
    }
}
fn send(process: &mut Process, kind: &str, id: &str, value: Value) -> Result<(), String> {
    let bytes = serde_json::to_vec(&json!({"version":1,"type":kind,"request_id":id,"value":value}))
        .map_err(|_| "无法编码启动响应")?;
    if bytes.len() > LIMIT {
        return Err("启动响应过大".into());
    }
    let input = process.input.as_mut().ok_or("后台正在关闭")?;
    input
        .write_all(&bytes)
        .and_then(|_| input.write_all(b"\n"))
        .and_then(|_| input.flush())
        .map_err(|_| "后台连接已关闭".into())
}
fn read_message(reader: &mut impl BufRead) -> Result<Value, ()> {
    let mut bytes = Vec::new();
    std::io::Read::take(reader, (LIMIT + 1) as u64)
        .read_until(b'\n', &mut bytes)
        .map_err(|_| ())?;
    if bytes.is_empty() || bytes.len() > LIMIT || bytes.last() != Some(&b'\n') {
        return Err(());
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
    let object = value.as_object().ok_or(())?;
    if value["version"].as_u64() != Some(1)
        || value["request_id"].as_str().is_none_or(|s| s.len() > 64)
    {
        return Err(());
    }
    let fields: &[&str] = match value["type"].as_str() {
        Some("trust_required") => &["workspace", "sources", "project_skills"],
        Some("configuration_required" | "closed") => &[],
        Some("mcp_required") => &["name", "transport", "source", "fingerprint"],
        Some("credentials_required") => &["names"],
        Some("ready") => &["port", "token"],
        Some("startup_failed") => &["code", "message"],
        Some("state") => &["active_run_id", "live_ids", "blockers"],
        _ => return Err(()),
    };
    if object.len() != fields.len() + 3
        || fields.iter().any(|key| !object.contains_key(*key))
        || object.keys().any(|key| {
            !["version", "type", "request_id"].contains(&key.as_str())
                && !fields.contains(&key.as_str())
        })
    {
        return Err(());
    }
    if value["type"] == "credentials_required" {
        let names = value["names"].as_array().ok_or(())?;
        if names.is_empty()
            || names.len() > 128
            || names.iter().any(|v| {
                v.as_str().is_none_or(|s| {
                    s.is_empty()
                        || s.len() > 128
                        || !s.bytes().enumerate().all(|(i, c)| {
                            c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit())
                        })
                })
            })
        {
            return Err(());
        }
    }
    let string_list = |key: &str| {
        value[key]
            .as_array()
            .is_some_and(|items| items.iter().all(|item| item.as_str().is_some()))
    };
    let valid = match value["type"].as_str() {
        Some("trust_required") => {
            value["workspace"].is_string()
                && string_list("sources")
                && value["project_skills"].is_boolean()
        }
        Some("mcp_required") => ["name", "transport", "source", "fingerprint"]
            .iter()
            .all(|key| value[*key].is_string()),
        Some("startup_failed") => value["code"].is_string() && value["message"].is_string(),
        Some("state") => {
            (value["active_run_id"].is_null() || value["active_run_id"].is_string())
                && string_list("live_ids")
                && string_list("blockers")
        }
        Some("ready") => {
            value["port"]
                .as_u64()
                .is_some_and(|p| p > 0 && p <= u16::MAX as u64)
                && value["token"].as_str().is_some_and(|s| {
                    s.len() >= 32
                        && s.len() <= 128
                        && s.bytes()
                            .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
                })
        }
        _ => true,
    };
    if !valid {
        return Err(());
    }
    Ok(value)
}
fn show(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
    }
}
fn home(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.navigate("tauri://localhost/index.html".parse().expect("bundled URL"));
        let _ = w.show();
    }
}
fn terminate_owned(child: &Arc<Mutex<Child>>, signal: i32) {
    let mut child = child.lock().expect("child lock");
    if matches!(child.try_wait(), Ok(None)) {
        // The unreaped, still-owned child is the only process group we can terminate.
        unsafe {
            libc::kill(-(child.id() as i32), signal);
        }
    }
}
fn shutdown(app: &AppHandle, state: &Shared) {
    let child = {
        let mut d = state.lock().expect("desktop lock");
        let Some(p) = d.process.as_mut() else {
            if d.quitting {
                app.exit(0);
            }
            return;
        };
        if p.shutdown {
            return;
        }
        p.shutdown = true;
        let _ = send(p, "shutdown", "", json!({}));
        p.input.take();
        p.child.clone()
    };
    thread::spawn(move || {
        thread::sleep(Duration::from_secs(30));
        terminate_owned(&child, libc::SIGTERM);
        thread::sleep(Duration::from_secs(2));
        terminate_owned(&child, libc::SIGKILL);
    });
}
fn request_action(app: &AppHandle, state: &Shared, action: &'static str) {
    let mut d = state.lock().expect("desktop lock");
    if let Some(p) = d.process.as_mut() {
        if p.action.is_some() || p.shutdown {
            return;
        }
        if p.port.is_none() {
            if action == "hide" {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            } else {
                d.quitting = action == "quit";
                drop(d);
                shutdown(app, state);
            }
            return;
        }
        p.action = Some(action);
        if send(p, "inspect", action, json!({})).is_err() {
            p.action = None;
        }
    } else if action == "quit" {
        d.quitting = true;
        app.exit(0);
    } else if action == "switch" {
        drop(d);
        home(app);
    } else if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
}
fn handle_state(app: &AppHandle, shared: &Shared, message: &Value) {
    let action = shared
        .lock()
        .expect("desktop lock")
        .process
        .as_mut()
        .and_then(|p| p.action.take());
    let Some(action) = action else {
        return;
    };
    if message["request_id"] != action {
        return;
    }
    let Some(live) = message["live_ids"].as_array() else {
        return;
    };
    let Some(blockers) = message["blockers"].as_array() else {
        return;
    };
    let busy = !message["active_run_id"].is_null() || !blockers.is_empty();
    if action == "hide" && live.is_empty() {
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.hide();
        }
        return;
    }
    if !busy && live.is_empty() {
        shared.lock().expect("desktop lock").quitting = action == "quit";
        shutdown(app, shared);
        return;
    }
    let app = app.clone();
    let shared = shared.clone();
    let text = if action == "hide" {
        "结束实时语音后关闭窗口？"
    } else {
        "当前项目有活动任务、语音或待处理操作。取消运行并离开？待处理记录将在重新打开时保留。"
    };
    let confirm = if action == "hide" {
        "结束语音并关闭"
    } else {
        "取消运行并离开"
    };
    app.clone()
        .dialog()
        .message(text)
        .buttons(MessageDialogButtons::OkCancelCustom(
            confirm.into(),
            "返回".into(),
        ))
        .show(move |ok| {
            if ok {
                if action == "hide" {
                    if let Some(p) = shared.lock().expect("desktop lock").process.as_mut() {
                        p.action = Some("hide");
                        let _ = send(p, "end_live", "hide", json!({}));
                    }
                } else {
                    shared.lock().expect("desktop lock").quitting = action == "quit";
                    shutdown(&app, &shared);
                }
            }
        });
}
fn launch(app: &AppHandle, shared: &Shared, workspace: &Path) -> Result<(), String> {
    let path = workspace.canonicalize().map_err(|_| "无法读取选定项目")?;
    if !path.is_dir() {
        return Err("请选择文件夹".into());
    }
    let resources = app.path().resource_dir().map_err(|_| "无法定位应用资源")?;
    if path.starts_with(&resources) {
        return Err("请选择应用包以外的项目目录".into());
    }
    let mut d = shared.lock().expect("desktop lock");
    if d.process.is_some() {
        return Err("请先从项目菜单切换工作区".into());
    }
    let backend = resources.join("backend/lumen-backend");
    let mut command = if cfg!(debug_assertions) && !backend.is_file() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut c = Command::new(root.join(".venv/bin/python"));
        c.args(["-m", "lumen.desktop"]);
        c
    } else {
        Command::new(backend)
    };
    command
        .arg("--cwd")
        .arg(&path)
        .current_dir(&path)
        .env_clear();
    for name in [
        "HOME",
        "USER",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ] {
        if let Some(v) = std::env::var_os(name) {
            command.env(name, v);
        }
    }
    command
        .env(
            "PATH",
            std::env::join_paths(
                d.preferences.tool_dirs.iter().cloned().chain(
                    [
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                    ]
                    .map(PathBuf::from),
                ),
            )
            .map_err(|_| "工具目录不能包含冒号")?,
        )
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = command
        .spawn()
        .map_err(|_| "无法启动内置后台，请重新构建或安装应用。")?;
    let input = child.stdin.take();
    let output = child.stdout.take().ok_or("后台缺少控制管道")?;
    let stderr = child.stderr.take().ok_or("后台缺少错误管道")?;
    let child = Arc::new(Mutex::new(child));
    d.error = None;
    d.process = Some(Process {
        child: child.clone(),
        input,
        port: None,
        pending: None,
        action: None,
        shutdown: false,
    });
    d.preferences.recent.retain(|p| p != &path);
    d.preferences.recent.insert(0, path);
    d.preferences.recent.truncate(8);
    save(app, &d);
    drop(d);
    // Third-party stderr may contain secrets; drain without persisting or exposing it.
    thread::spawn(move || {
        let _ = std::io::copy(&mut BufReader::new(stderr), &mut std::io::sink());
    });
    let app = app.clone();
    let shared = shared.clone();
    thread::spawn(move || {
        let mut reader = BufReader::new(output);
        while let Ok(message) = read_message(&mut reader) {
            let mut d = shared.lock().expect("desktop lock");
            let Some(p) = d.process.as_mut() else {
                break;
            };
            match message["type"].as_str() {
                Some("ready") => {
                    let port = message["port"]
                        .as_u64()
                        .filter(|p| *p > 0 && *p <= u16::MAX as u64);
                    let token = message["token"].as_str().filter(|s| {
                        s.len() >= 32
                            && s.len() <= 128
                            && s.bytes()
                                .all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
                    });
                    if p.port.is_some() || p.pending.is_some() || port.is_none() || token.is_none()
                    {
                        break;
                    }
                    p.port = port.map(|p| p as u16);
                    let url = format!(
                        "http://127.0.0.1:{}/?token={}",
                        port.unwrap(),
                        token.unwrap()
                    );
                    drop(d);
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.navigate(url.parse().expect("validated URL"));
                    }
                }
                Some("state") => {
                    drop(d);
                    handle_state(&app, &shared, &message);
                }
                Some("closed") => break,
                Some("startup_failed") => {
                    d.error = Some("启动失败。请检查配置和外部工具后重新打开项目。".into());
                }
                Some(
                    "trust_required"
                    | "mcp_required"
                    | "credentials_required"
                    | "configuration_required",
                ) => {
                    if p.port.is_some() || p.pending.is_some() || message["request_id"] == "" {
                        break;
                    }
                    p.pending = Some(message.clone());
                    if message["type"] == "credentials_required" {
                        let credentials = message.clone();
                        let app = app.clone();
                        let shared = shared.clone();
                        thread::spawn(move || {
                            let names = credentials["names"].as_array().expect("validated names");
                            let values: BTreeMap<String, String> = names
                                .iter()
                                .filter_map(|name| {
                                    let name = name.as_str()?;
                                    let bytes =
                                        security_framework::passwords::get_generic_password(
                                            SERVICE, name,
                                        )
                                        .ok()?;
                                    let value = String::from_utf8(bytes).ok()?;
                                    (!value.is_empty()).then(|| (name.into(), value))
                                })
                                .collect();
                            if values.len() != names.len() {
                                return;
                            }
                            let mut d = shared.lock().expect("desktop lock");
                            let Some(p) = d.process.as_mut() else {
                                return;
                            };
                            if p.shutdown
                                || p.pending.as_ref().is_none_or(|pending| {
                                    pending["request_id"] != credentials["request_id"]
                                })
                            {
                                return;
                            }
                            if send(
                                p,
                                "reply",
                                credentials["request_id"].as_str().expect("ID"),
                                json!({"values":values}),
                            )
                            .is_ok()
                            {
                                p.pending = None;
                                let _ = app.emit("desktop-event", json!({"type":"starting"}));
                            }
                        });
                    }
                    let _ = app.emit("desktop-event", &message);
                }
                _ => break,
            }
        }
        shared
            .lock()
            .expect("desktop lock")
            .process
            .as_mut()
            .map(|p| p.input.take());
        let start = Instant::now();
        loop {
            if !matches!(child.lock().expect("child lock").try_wait(), Ok(None)) {
                break;
            }
            if start.elapsed() > Duration::from_secs(30) {
                terminate_owned(&child, libc::SIGKILL);
            }
            thread::sleep(Duration::from_millis(50));
        }
        let mut d = shared.lock().expect("desktop lock");
        let expected = d.process.as_ref().is_some_and(|p| p.shutdown);
        d.process = None;
        if !expected && d.error.is_none() {
            d.error = Some("后台连接已结束。项目记录已保留，请重新打开项目。".into());
        }
        if d.quitting {
            app.exit(0);
        } else {
            drop(d);
            home(&app);
        }
    });
    Ok(())
}
#[tauri::command]
fn bootstrap(window: WebviewWindow, state: tauri::State<Shared>) -> Result<Value, String> {
    guard(&window)?;
    let d = state.lock().map_err(|_| "桌面状态不可用")?;
    Ok(
        json!({"recent":d.preferences.recent,"pending":d.process.as_ref().and_then(|p| p.pending.clone()),
        "running":d.process.is_some(),"error":d.error}),
    )
}
#[tauri::command]
async fn choose_project(
    window: WebviewWindow,
    app: AppHandle,
    state: tauri::State<'_, Shared>,
    path: Option<PathBuf>,
) -> Result<bool, String> {
    guard(&window)?;
    let workspace = if let Some(path) = path {
        if !state
            .lock()
            .map_err(|_| "桌面状态不可用")?
            .preferences
            .recent
            .contains(&path)
        {
            return Err("该项目不在最近列表中，请重新选择文件夹".into());
        }
        Some(path)
    } else {
        let app = app.clone();
        tauri::async_runtime::spawn_blocking(move || {
            app.dialog()
                .file()
                .set_title("选择 Lumen 项目")
                .blocking_pick_folder()
                .and_then(|p| p.into_path().ok())
        })
        .await
        .map_err(|_| "目录选择失败")?
    };
    if let Some(path) = workspace {
        launch(&app, &state, &path)?;
        return Ok(true);
    }
    Ok(false)
}
#[tauri::command]
async fn reply(
    window: WebviewWindow,
    state: tauri::State<'_, Shared>,
    request_id: String,
    value: Value,
    remember: bool,
) -> Result<(), String> {
    guard(&window)?;
    let shared = state.inner().clone();
    let pending = {
        let d = shared.lock().map_err(|_| "桌面状态不可用")?;
        let p = d.process.as_ref().ok_or("后台已关闭")?;
        if p.shutdown {
            return Err("后台正在关闭".into());
        }
        let pending = p.pending.as_ref().ok_or("没有待处理启动请求")?;
        if pending["request_id"] != request_id {
            return Err("启动请求已过期".into());
        }
        pending.clone()
    };
    if serde_json::to_vec(&value)
        .map_err(|_| "响应格式无效")?
        .len()
        > LIMIT - 256
    {
        return Err("启动响应过大".into());
    }
    if pending["type"] == "credentials_required" {
        let values: BTreeMap<String, String> =
            serde_json::from_value(value["values"].clone()).map_err(|_| "凭据格式无效")?;
        let names = pending["names"].as_array().ok_or("凭据请求无效")?;
        if value.as_object().is_none_or(|v| v.len() != 1)
            || values.len() != names.len()
            || names.iter().any(|n| {
                n.as_str()
                    .is_none_or(|n| values.get(n).is_none_or(|v| v.is_empty()))
            })
        {
            return Err("请提供所有必需凭据".into());
        }
        if remember {
            // OS authorization must never block the UI thread or the desktop state lock.
            tauri::async_runtime::spawn_blocking(move || {
                for (name, secret) in &values {
                    security_framework::passwords::set_generic_password(
                        SERVICE,
                        name,
                        secret.as_bytes(),
                    )
                    .map_err(|_| "无法保存到 Keychain，请解锁钥匙串或取消保存后重试。")?;
                }
                Ok::<(), String>(())
            })
            .await
            .map_err(|_| "钥匙串操作失败")??;
        }
    } else if remember {
        return Err("此请求不能保存凭据".into());
    }
    let mut d = shared.lock().map_err(|_| "桌面状态不可用")?;
    let p = d.process.as_mut().ok_or("后台已关闭")?;
    if p.shutdown
        || p.pending
            .as_ref()
            .is_none_or(|pending| pending["request_id"] != request_id)
    {
        return Err("启动请求已过期".into());
    }
    send(p, "reply", &request_id, value)?;
    p.pending = None;
    Ok(())
}
#[tauri::command]
fn cancel_startup(
    window: WebviewWindow,
    app: AppHandle,
    state: tauri::State<Shared>,
) -> Result<(), String> {
    guard(&window)?;
    shutdown(&app, &state);
    Ok(())
}
#[tauri::command]
async fn forget_credential(window: WebviewWindow, name: String) -> Result<(), String> {
    guard(&window)?;
    if name.is_empty()
        || name.len() > 128
        || !name
            .bytes()
            .enumerate()
            .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
    {
        return Err("凭据名称无效".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        match security_framework::passwords::delete_generic_password(SERVICE, &name) {
            Ok(()) => Ok(()),
            Err(e) if e.code() == -25300 => Ok(()),
            Err(_) => Err("无法删除 Keychain 凭据，请在钥匙串访问中检查权限。".into()),
        }
    })
    .await
    .map_err(|_| "钥匙串操作失败")?
}
fn main() {
    let shared: Shared = Arc::default();
    let builder = tauri::Builder::default().manage(shared.clone())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| show(app)))
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![bootstrap, choose_project, reply, cancel_startup, forget_credential])
        .setup(move |app| {
            if let Some(path) = preferences(app.handle()) {
                if let Ok(bytes) = std::fs::read(path) { if bytes.len() <= LIMIT {
                    if let Ok(mut prefs) = serde_json::from_slice::<Preferences>(&bytes) {
                        prefs.recent.truncate(8); shared.lock().expect("desktop lock").preferences = prefs;
                    }
                }}
            }
            let size = { let d = shared.lock().expect("desktop lock");
                (d.preferences.width.unwrap_or(1180.).clamp(800., 2560.), d.preferences.height.unwrap_or(820.).clamp(600., 1800.)) };
            let navigation = shared.clone();
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Lumen").inner_size(size.0, size.1).min_inner_size(800., 600.)
                .on_navigation(move |url| {
                    if local(url) { return true; }
                    let d = navigation.lock().expect("desktop lock");
                    if url.scheme() == "http" && url.host_str() == Some("127.0.0.1")
                        && d.process.as_ref().and_then(|p| p.port) == url.port() { return true; } drop(d);
                    if ["http", "https"].contains(&url.scheme()) { let _ = Command::new("/usr/bin/open").arg(url.as_str()).spawn(); }
                    false
                }).on_new_window(|url, _| {
                    if ["http", "https"].contains(&url.scheme()) { let _ = Command::new("/usr/bin/open").arg(url.as_str()).spawn(); }
                    tauri::webview::NewWindowResponse::Deny
                }).build()?;
            let state = shared.clone(); let handle = app.handle().clone();
            window.on_window_event(move |event| match event {
                WindowEvent::CloseRequested { api, .. } => { api.prevent_close(); request_action(&handle, &state, "hide"); }
                WindowEvent::Resized(size) => { if let Some(w) = handle.get_webview_window("main") {
                    let scale = w.scale_factor().unwrap_or(1.); let mut d = state.lock().expect("desktop lock");
                    d.preferences.width = Some(size.width as f64 / scale); d.preferences.height = Some(size.height as f64 / scale); save(&handle, &d);
                }}, _ => {}
            });
            let show = MenuItem::with_id(app, "show", "显示 Lumen", true, None::<&str>)?;
            let switch = MenuItem::with_id(app, "switch", "切换项目…", true, Some("CmdOrCtrl+O"))?;
            let quit = MenuItem::with_id(app, "quit", "退出 Lumen", true, Some("CmdOrCtrl+Q"))?;
            let about = MenuItem::with_id(app, "about", "关于 Lumen", true, None::<&str>)?;
            let app_menu = Submenu::with_items(app, "Lumen", true, &[&about, &show, &PredefinedMenuItem::separator(app)?, &quit])?;
            let projects = Submenu::with_items(app, "项目", true, &[&switch])?;
            let tools = MenuItem::with_id(app, "tools", "添加外部工具目录…", true, None::<&str>)?;
            let update = MenuItem::with_id(app, "update", "下载新版本…", true, None::<&str>)?;
            let settings = Submenu::with_items(app, "设置", true, &[&tools, &update])?;
            let edit = Submenu::with_items(app, "编辑", true, &[&PredefinedMenuItem::undo(app, None)?,
                &PredefinedMenuItem::redo(app, None)?, &PredefinedMenuItem::separator(app)?, &PredefinedMenuItem::cut(app, None)?,
                &PredefinedMenuItem::copy(app, None)?, &PredefinedMenuItem::paste(app, None)?, &PredefinedMenuItem::select_all(app, None)?])?;
            app.set_menu(Menu::with_items(app, &[&app_menu, &projects, &edit, &settings])?)?; Ok(())
        }).on_menu_event(|app, event| { let state = app.state::<Shared>(); match event.id().as_ref() {
            "show" => show(app), "switch" => request_action(app, &state, "switch"), "quit" => request_action(app, &state, "quit"),
            "update" => { let _ = Command::new("/usr/bin/open").arg("https://github.com/JasonYZzz/lumen/releases").spawn(); }
            "tools" => {
                let handle = app.clone();
                app.dialog().file().set_title("选择外部工具所在文件夹").pick_folder(move |folder| {
                    if let Some(path) = folder.and_then(|f| f.into_path().ok()).and_then(|p| p.canonicalize().ok())
                        .filter(|p| p.is_dir()) {
                        if std::env::join_paths([&path]).is_err() { return; }
                        let state = handle.state::<Shared>(); let mut d = state.lock().expect("desktop lock");
                        if !d.preferences.tool_dirs.contains(&path) && d.preferences.tool_dirs.len() < 16 {
                            d.preferences.tool_dirs.push(path); save(&handle, &d);
                        }
                        drop(d); handle.dialog().message("工具目录已保存，将在下次打开项目时使用。").show(|_| {});
                    }
                });
            }
            "about" => app.dialog().message("Lumen 0.1.0 · Apple Silicon\n开源桌面版 · 手动更新\nPython Runtime · Tauri 2").show(|_| {}), _ => {}
        }});
    let app = builder
        .build(tauri::generate_context!())
        .expect("Lumen desktop initialization");
    app.run(|app, event| match event {
        RunEvent::ExitRequested { api, .. } => {
            let state = app.state::<Shared>();
            if !state.lock().expect("desktop lock").quitting {
                api.prevent_exit();
                request_action(app, &state, "quit");
            }
        }
        RunEvent::Reopen { .. } => show(app),
        _ => {}
    });
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn protocol_is_bounded_and_rejects_unknown_fields() {
        for input in [b"{\"version\":1,\"type\":\"closed\",\"request_id\":\"\",\"token\":\"secret\"}\n".to_vec(),
            vec![b'x'; LIMIT + 1], b"{\"version\":1,\"type\":\"credentials_required\",\"request_id\":\"a\",\"names\":[\"../secret\"]}\n".to_vec()] {
            assert!(read_message(&mut std::io::Cursor::new(input)).is_err());
        }
        assert!(read_message(&mut std::io::Cursor::new(
            b"{\"version\":1,\"type\":\"closed\",\"request_id\":\"\"}\n"
        ))
        .is_ok());
        for message in [
            json!({"type":"state", "active_run_id": false, "live_ids":[], "blockers":[]}),
            json!({"type":"ready", "port":65536, "token":"a".repeat(32)}),
            json!({"type":"trust_required", "workspace":"/tmp", "sources":[null], "project_skills":false}),
        ] {
            let mut message = message;
            message["version"] = json!(1);
            message["request_id"] = json!("a");
            let bytes = format!("{message}\n");
            assert!(read_message(&mut std::io::Cursor::new(bytes)).is_err());
        }
    }
    #[test]
    fn local_origin_is_exact() {
        assert!(local(&"tauri://localhost/index.html".parse().unwrap()));
        for url in [
            "http://127.0.0.1:8765",
            "http://tauri.localhost:9999",
            "https://tauri.localhost",
            "tauri://evil",
        ] {
            assert!(!local(&url.parse().unwrap()));
        }
    }
}
