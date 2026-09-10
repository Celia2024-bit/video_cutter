# 怎么生成 Windows / Mac 安装包

给别人用的是打包好的程序，对方**不用装 Python、ffmpeg、pip**。  
Windows 包只能在 Windows 上编，Mac 包只能在 Mac 上编，**不能交叉编译**。

先进入仓库根目录（里面有 `web.py` 的那一层）。

---

## Windows

本机需要：已安装的 Python 3，以及能上网（第一次会下载依赖和 ffmpeg）。

在 PowerShell 里执行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

脚本会：

1. 在 `packaging/.venv` 建干净虚拟环境
2. 安装 Flask、faster-whisper、PyInstaller 等
3. 下载并打进 ffmpeg
4. 用 PyInstaller 生成 exe
5. 打成 zip

### 编出来的文件

| 文件 | 干什么 |
| --- | --- |
| `dist/VideoCutter/VideoCutter.exe` | 本机双击测试 |
| `dist/VideoCutter-windows.zip` | **发给 Windows 用户的就是这个** |

若本机装了 [Inno Setup 6](https://jrsoftware.org/isinfo.php)，脚本还会多生成：

- `dist/VideoCutter-Setup.exe`（下一步、下一步那种安装向导）

没装 Inno Setup 也不影响，zip 就够用。

### 对方怎么用

解压 zip 后任选一种：

- 直接双击 `VideoCutter.exe`（免安装）
- 双击 `Install-VideoCutter.cmd`：拷到 `%LOCALAPPDATA%\Programs\VideoCutter`，并在桌面放快捷方式

点开后会出现一个黑色窗口，并自动打开浏览器 `http://127.0.0.1:8770`。  
**关掉黑色窗口就是退出程序。**

---

## Mac

本机需要：已安装的 Python 3。ffmpeg 脚本会尽量从 PATH 复制，没有的话会尝试下载。

在终端执行：

```bash
chmod +x packaging/macos/build.sh
./packaging/macos/build.sh
```

### 编出来的文件

| 文件 | 干什么 |
| --- | --- |
| `dist/Video Cutter.app` | 本机双击测试 |
| `dist/VideoCutter-macos.dmg` | **发给 Mac 用户的就是这个** |

两个不是两套程序，DMG 里面就是那个 App。**只发 DMG**，不要两个都发。

### 对方怎么用

1. 双击打开 DMG
2. 把 Video Cutter 拖到「应用程序」，或直接运行
3. 第一次若提示来自身份不明的开发者：**右键 App → 打开**

没做苹果开发者签名，所以第一次几乎都会被拦截，右键打开一次之后就好了。

---

## 发给别人时带哪个

| 对方系统 | 你发这个 | 不要发 |
| --- | --- | --- |
| Windows | `VideoCutter-windows.zip`（或 `VideoCutter-Setup.exe`） | Mac 的 dmg / app |
| Mac | `VideoCutter-macos.dmg` | Windows 的 zip / exe |

对方剪视频、导出、烧字幕都不需要再装环境。

只有语音转文字有额外条件：

- **本地转写**：第一次会从网上下载 Whisper 模型，之后可离线。模型没有打进安装包。
- **Groq 云端转写**：要在页面里填 Groq API key。不用 Groq 就不用管。
