#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::UI::WindowsAndMessaging::{MessageBoxW, MB_ICONERROR, MB_OK};

const WORKBENCH_PORT: u16 = 8766;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn selected_profile() -> Result<String, String> {
    let profile = env::var("AGENT4COMPANY_PROFILE").unwrap_or_else(|_| "company-manager".into());
    match profile.as_str() {
        "company-manager" | "company-with-sales" => Ok(profile),
        _ => Err(format!("未知工作台组合：{profile}")),
    }
}

#[derive(Default)]
struct RuntimeChildren(Mutex<Vec<Child>>);

fn is_project_root(path: &Path) -> bool {
    path.join("company_platform/__main__.py").is_file()
        && path.join("profiles/company-manager/profile.json").is_file()
        && path.join("ui/index.html").is_file()
        && path.join("scripts/start-pi-windows.ps1").is_file()
}

#[cfg(windows)]
fn usable_windows_path(path: PathBuf) -> PathBuf {
    let value = path.to_string_lossy();
    if let Some(stripped) = value.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{stripped}"));
    }
    if let Some(stripped) = value.strip_prefix(r"\\?\") {
        return PathBuf::from(stripped);
    }
    path
}

#[cfg(not(windows))]
fn usable_windows_path(path: PathBuf) -> PathBuf {
    path
}

fn project_root() -> Result<PathBuf, String> {
    let mut starts = Vec::new();
    if let Ok(current) = env::current_dir() {
        starts.push(usable_windows_path(current));
    }
    if let Ok(executable) = env::current_exe() {
        if let Some(parent) = executable.parent() {
            starts.push(usable_windows_path(parent.to_path_buf()));
        }
    }
    for start in starts {
        let mut cursor = Some(start.as_path());
        for _ in 0..9 {
            let Some(path) = cursor else { break };
            if is_project_root(path) {
                return Ok(path.to_path_buf());
            }
            cursor = path.parent();
        }
    }
    Err("Agent4Company 必须从完整的公司管理平台目录启动。".into())
}

fn runtime_log(root: &Path, name: &str, truncate: bool) -> Result<File, String> {
    let path = root.join(".pi/company-runtime").join(name);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    let mut options = OpenOptions::new();
    options.create(true).write(true);
    if truncate {
        options.truncate(true);
    } else {
        options.append(true);
    }
    options.open(path).map_err(|error| error.to_string())
}

fn log_event(root: &Path, event: &str) {
    if let Ok(mut output) = runtime_log(root, "desktop-launcher.log", false) {
        let _ = writeln!(output, "[desktop pid={}] {event}", std::process::id());
    }
}

fn python_command(root: &Path) -> Result<(String, Vec<String>), String> {
    let local = if cfg!(windows) {
        root.join(".venv/Scripts/python.exe")
    } else {
        root.join(".venv/bin/python")
    };
    if local.is_file() {
        return Ok((local.to_string_lossy().to_string(), Vec::new()));
    }
    let candidates = if cfg!(windows) {
        vec![("python.exe", vec![]), ("py.exe", vec!["-3.11"])]
    } else {
        vec![("python3", vec![]), ("python", vec![])]
    };
    for (program, prefix) in candidates {
        if Command::new(program)
            .args(&prefix)
            .args([
                "-c",
                "import sys; raise SystemExit(sys.version_info < (3, 11))",
            ])
            .output()
            .is_ok_and(|output| output.status.success())
        {
            return Ok((
                program.into(),
                prefix.into_iter().map(String::from).collect(),
            ));
        }
    }
    Err("未找到 Python 3.11+；请先运行 scripts/setup-windows.ps1。".into())
}

fn workbench_healthy(expected_profile: &str) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], WORKBENCH_PORT));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(700)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(900)));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:8766\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = Vec::with_capacity(4096);
    let _ = stream.take(4096).read_to_end(&mut response);
    let text = String::from_utf8_lossy(&response);
    text.contains("200 OK")
        && text.contains("\"product_id\": \"agent4company\"")
        && text.contains(&format!("\"profile_id\": \"{expected_profile}\""))
}

fn start_workbench(root: &Path, profile: &str) -> Result<Child, String> {
    if workbench_healthy(profile) {
        return Err("端口 8766 已有工作台运行；请先退出旧版本。".into());
    }
    let (program, mut arguments) = python_command(root)?;
    arguments.extend([
        "-m".into(),
        "company_platform".into(),
        "serve".into(),
        "--port".into(),
        WORKBENCH_PORT.to_string(),
        "--profile".into(),
        profile.into(),
    ]);
    let output = runtime_log(root, "workbench.log", true)?;
    let error = output.try_clone().map_err(|value| value.to_string())?;
    let mut command = Command::new(program);
    command
        .args(arguments)
        .current_dir(root)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::from(output))
        .stderr(Stdio::from(error));
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    command
        .spawn()
        .map_err(|error| format!("公司工作台启动失败：{error}"))
}

fn wait_for_workbench(child: &mut Child, profile: &str) -> bool {
    let deadline = Instant::now() + Duration::from_secs(35);
    while Instant::now() < deadline {
        if workbench_healthy(profile) {
            return true;
        }
        if child.try_wait().ok().flatten().is_some() {
            return false;
        }
        thread::sleep(Duration::from_millis(250));
    }
    false
}

#[cfg(windows)]
fn start_pi(root: &Path, profile: &str) -> Result<Child, String> {
    let output = runtime_log(root, "pi-core.log", true)?;
    let error = output.try_clone().map_err(|value| value.to_string())?;
    let mut command = Command::new("powershell.exe");
    command
        .args([
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            &root.join("scripts/start-pi-windows.ps1").to_string_lossy(),
            "--mode",
            "rpc",
            "--approve",
        ])
        .current_dir(root)
        .env("AGENT4COMPANY_PROFILE", profile)
        .stdin(Stdio::piped())
        .stdout(Stdio::from(output))
        .stderr(Stdio::from(error))
        .creation_flags(CREATE_NO_WINDOW);
    command
        .spawn()
        .map_err(|error| format!("Pi 公司级智能核心启动失败：{error}"))
}

#[cfg(not(windows))]
fn start_pi(_root: &Path, _profile: &str) -> Result<Child, String> {
    Err("第一阶段只交付 Windows Pi 桌面启动链路。".into())
}

#[cfg(windows)]
fn stop_child(child: &mut Child) {
    let _ = Command::new("taskkill.exe")
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(not(windows))]
fn stop_child(child: &mut Child) {
    let _ = child.kill();
}

fn cleanup(children: &RuntimeChildren) {
    if let Ok(mut locked) = children.0.lock() {
        for child in locked.iter_mut().rev() {
            stop_child(child);
        }
        locked.clear();
    }
}

fn python_self_test(root: &Path) -> bool {
    let Ok((program, mut arguments)) = python_command(root) else {
        return false;
    };
    arguments.extend(["-m".into(), "company_platform".into(), "self-test".into()]);
    Command::new(program)
        .args(arguments)
        .current_dir(root)
        .output()
        .is_ok_and(|output| output.status.success())
}

fn pi_version_ok(root: &Path) -> bool {
    let command = if cfg!(windows) {
        root.join("node_modules/.bin/pi.CMD")
    } else {
        root.join("node_modules/.bin/pi")
    };
    command.is_file()
        && Command::new(command)
            .arg("--version")
            .current_dir(root)
            .output()
            .is_ok_and(|output| output.status.success())
}

fn self_test() -> i32 {
    let root = match project_root() {
        Ok(root) => root,
        Err(error) => {
            eprintln!("Agent4Company 自检无法定位项目：{error}");
            return 2;
        }
    };
    let profile = match selected_profile() {
        Ok(profile) => profile,
        Err(error) => {
            eprintln!("Agent4Company 自检 Profile 无效：{error}");
            return 2;
        }
    };
    let mut server = match start_workbench(&root, &profile) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("Agent4Company 自检无法启动工作台：{error}");
            return 2;
        }
    };
    let healthy = wait_for_workbench(&mut server, &profile);
    let runtime_ok = healthy && python_self_test(&root);
    let pi_ok = runtime_ok && pi_version_ok(&root);
    stop_child(&mut server);
    if healthy && runtime_ok && pi_ok {
        0
    } else {
        eprintln!("Agent4Company 自检失败：workbench={healthy}, runtime={runtime_ok}, pi={pi_ok}");
        2
    }
}

#[cfg(windows)]
fn show_startup_error(message: &str) {
    let title: Vec<u16> = "公司管理平台启动失败\0".encode_utf16().collect();
    let body: Vec<u16> = format!(
        "公司管理平台未能启动。\n\n{message}\n\n请运行 scripts\\setup-windows.ps1 后重试；详细记录位于 .pi\\company-runtime。\0"
    )
    .encode_utf16()
    .collect();
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            body.as_ptr(),
            title.as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

#[cfg(not(windows))]
fn show_startup_error(message: &str) {
    eprintln!("公司管理平台启动失败：{message}");
}

fn main() {
    if env::args().any(|argument| argument == "--self-test") {
        std::process::exit(self_test());
    }
    let application = tauri::Builder::default()
        .manage(RuntimeChildren::default())
        .plugin(tauri_plugin_single_instance::init(
            |app, _arguments, _cwd| {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            },
        ))
        .setup(|app| {
            let root = project_root().map_err(std::io::Error::other)?;
            let profile = selected_profile().map_err(std::io::Error::other)?;
            env::set_current_dir(&root)?;
            log_event(&root, &format!("startup began with profile={profile}"));
            let mut workbench = start_workbench(&root, &profile).map_err(std::io::Error::other)?;
            if !wait_for_workbench(&mut workbench, &profile) {
                stop_child(&mut workbench);
                return Err(std::io::Error::other("公司工作台健康检查失败").into());
            }
            let window = match WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::External("http://127.0.0.1:8766/".parse().expect("静态本地 URL")),
            )
            .title("公司管理平台")
            .inner_size(1380.0, 900.0)
            .min_inner_size(980.0, 680.0)
            .center()
            .on_navigation(|url| {
                url.scheme() == "http"
                    && url.host_str() == Some("127.0.0.1")
                    && url.port() == Some(WORKBENCH_PORT)
            })
            .build()
            {
                Ok(window) => window,
                Err(error) => {
                    stop_child(&mut workbench);
                    return Err(error.into());
                }
            };
            let mut pi = match start_pi(&root, &profile) {
                Ok(child) => child,
                Err(error) => {
                    let _ = window.close();
                    stop_child(&mut workbench);
                    return Err(std::io::Error::other(error).into());
                }
            };
            thread::sleep(Duration::from_millis(750));
            if pi.try_wait()?.is_some() {
                let _ = window.close();
                stop_child(&mut workbench);
                return Err(std::io::Error::other(
                    "Pi 公司级智能核心启动后提前退出；请查看 .pi/company-runtime/pi-core.log",
                )
                .into());
            }
            let state = app.state::<RuntimeChildren>();
            match state.0.lock() {
                Ok(mut children) => {
                    children.push(workbench);
                    children.push(pi);
                }
                Err(_) => {
                    let _ = window.close();
                    stop_child(&mut pi);
                    stop_child(&mut workbench);
                    return Err(std::io::Error::other("桌面进程锁已损坏").into());
                }
            }
            log_event(&root, "workbench and Pi core ready");
            Ok(())
        })
        .build(tauri::generate_context!());

    let application = match application {
        Ok(application) => application,
        Err(error) => {
            show_startup_error(&error.to_string());
            return;
        }
    };
    application.run(|app, event| match event {
        RunEvent::WindowEvent {
            label,
            event: WindowEvent::CloseRequested { .. },
            ..
        } if label == "main" => {
            cleanup(&app.state::<RuntimeChildren>());
            app.exit(0);
        }
        RunEvent::Exit => cleanup(&app.state::<RuntimeChildren>()),
        _ => {}
    });
}
