"""kabuステーションのチャート画面を指定銘柄に切り替える（実験的・Windows専用）。

公式のAPIには画面を操作する機能がないため、Windowsの画面操作機能（UI Automation）で
チャート画面の銘柄コード欄を探して書き換える。誤操作を避けるため、入力先が
「チャート」画面の中の銘柄コード欄だと確認できた場合にだけキーを送る。
注文画面など、それ以外の場所には一切入力しない。
"""
from __future__ import annotations

import os
import logging
import subprocess
import sys
import threading

log = logging.getLogger(__name__)
_lock = threading.Lock()

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
$code = $env:CANDLE_CODE
$inspect = $env:CANDLE_INSPECT -eq '1'
$AE = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
$all = [System.Windows.Automation.Condition]::TrueCondition
$editType = [System.Windows.Automation.ControlType]::Edit
$valuePattern = [System.Windows.Automation.ValuePattern]::Pattern

function Children($e) { try { return $e.FindAll($scope::Children, $all) } catch { return @() } }
function Val($e) {
  try { $p = $null; if ($e.TryGetCurrentPattern($valuePattern, [ref]$p)) { return $p.Current.Value } } catch {}
  return $null
}
function Dump($e, $depth) {
  if ($depth -gt 7 -or $script:lines -gt 300) { return }
  $script:lines++
  $c = $e.Current
  ('  ' * $depth) + $c.ControlType.ProgrammaticName.Replace('ControlType.', '') + ' name=[' + $c.Name + '] id=[' + $c.AutomationId + '] class=[' + $c.ClassName + '] value=[' + (Val $e) + ']'
  foreach ($k in (Children $e)) { Dump $k ($depth + 1) }
}

# kabuステーション本体と、チャート画面（題名が「チャート:3436/T」の形のもの）を探す。
# ツールバーの「チャート」ボタンなどを拾わないよう、題名に「:」を含むものだけを対象にする。
function IsChart($name) { return ($name -match '^チャート\s*[:：]') }
$charts = @(); $main = $null
foreach ($w in (Children $AE::RootElement)) {
  $n = $w.Current.Name
  if (IsChart $n) { $charts += $w }
  if ($n -like 'kabuステーション*') { $main = $w }
}
if (-not $main -and $charts.Count -eq 0) { 'NG:kabuステーションの画面が見つかりません。起動しているか確認してください。'; exit 2 }
if ($main) {
  $queue = @(@($main, 0))
  while ($queue.Count -gt 0) {
    $item = $queue[0]; $queue = @($queue | Select-Object -Skip 1)
    if ($item[1] -ge 6) { continue }
    foreach ($k in (Children $item[0])) {
      if (IsChart $k.Current.Name) { $charts += $k } else { $queue += , @($k, ($item[1] + 1)) }
    }
  }
}

if ($inspect) {
  'チャート画面の候補: ' + $charts.Count
  $script:lines = 0
  foreach ($c in $charts) { '--- ' + $c.Current.Name; Dump $c 0 }
  if ($charts.Count -eq 0 -and $main) { '--- 本体（上位の階層のみ）'; Dump $main 3 }
  exit 0
}
if ($charts.Count -eq 0) { 'NG:チャート画面が見つかりません。kabuステーションでチャートを1つ開いておいてください。'; exit 3 }

# 株式のチャート（題名が「チャート：2914/T」のように4桁コード）だけを対象にする。
# 先物などのチャート（「チャート：101.2612/O」）は書き換えない。
$chart = $null
foreach ($c in $charts) { if ($c.Current.Name -match '[:：]\s*[0-9A-Za-z]{4}/') { $chart = $c; break } }
if (-not $chart) { 'NG:株式のチャート画面が見つかりません。kabuステーションで株式のチャートを1つ開いておいてください。'; exit 3 }

# 銘柄コード欄は、コンボボックスの中の入力欄（id が "[Editor] Edit Area"）
$edit = $null
$edits = $chart.FindAll($scope::Descendants, (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $editType)))
foreach ($e in $edits) { if ($e.Current.AutomationId -eq '[Editor] Edit Area') { $edit = $e; break } }
if (-not $edit) { foreach ($e in $edits) { if ((Val $e) -match '^[0-9A-Za-z]{4}(/[0-9A-Za-z]+)?$') { $edit = $e; break } } }
if (-not $edit) { 'NG:チャート画面の銘柄コード欄が見つかりません。'; exit 4 }
$combo = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($edit)

try { (New-Object -ComObject WScript.Shell).AppActivate($main.Current.ProcessId) | Out-Null } catch {}
Start-Sleep -Milliseconds 200
$edit.SetFocus()
Start-Sleep -Milliseconds 200

# 入力先がその銘柄コード欄であることを確認できた場合だけキーを送る
$focused = $AE::FocusedElement
$same = $false
try {
  $w = [System.Windows.Automation.TreeWalker]::RawViewWalker; $f = $focused
  for ($i = 0; $i -lt 3 -and $f -and -not $same; $i++) {   # 欄そのもの、その内側、または欄を含むコンボボックス
    $id = $f.GetRuntimeId()
    $same = [System.Windows.Automation.Automation]::Compare($id, $edit.GetRuntimeId()) -or ($combo -and [System.Windows.Automation.Automation]::Compare($id, $combo.GetRuntimeId()))
    $f = $w.GetParent($f)
  }
} catch {}
if (-not $same) { 'NG:銘柄コード欄を選択できなかったため、何も入力しませんでした。'; exit 5 }

$set = $false
try { $p = $null; if ($edit.TryGetCurrentPattern($valuePattern, [ref]$p) -and -not $p.Current.IsReadOnly) { $p.SetValue($code); $set = $true } } catch {}
if ($set) { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
else { [System.Windows.Forms.SendKeys]::SendWait('^a' + $code + '{ENTER}') }
# 題名が指定の銘柄に変わったかを確かめる
$done = $false
for ($i = 0; $i -lt 10 -and -not $done; $i++) { Start-Sleep -Milliseconds 200; try { $done = $chart.Current.Name -match ('[:：]\s*' + $code + '/') } catch {} }
if ($done) { 'OK:' + $code } else { 'NG:銘柄コードを入力しましたが、チャートの切り替わりを確認できませんでした。' }
"""


def show_chart(symbol: str, inspect: bool = False) -> tuple[bool, str]:
    """チャートを切り替える。戻り値は (成功したか, 説明)。inspect=True は画面構造の調査用。"""
    if not symbol.isalnum() or len(symbol) > 6:
        return False, "銘柄コードが不正です"
    if sys.platform != "win32":
        return False, "この機能はWindowsでのみ使えます"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            env={**os.environ, "CANDLE_CODE": symbol, "CANDLE_INSPECT": "1" if inspect else "0"},
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return False, f"実行に失敗しました: {e}"
    out = (r.stdout or b"").decode("cp932", "replace").strip()
    err = (r.stderr or b"").decode("cp932", "replace").strip()
    if inspect:
        return r.returncode == 0, out or err
    if out.startswith("OK:"):
        return True, f"{symbol} に切り替えました"
    return False, (out[3:] if out.startswith("NG:") else (out or err or "原因不明の失敗"))[:300]


def show_chart_async(symbol: str) -> None:
    """受信処理を止めずに切り替える。重なった場合は順番に実行する。"""
    def run():
        with _lock:
            ok, message = show_chart(symbol)
            log.info("チャート自動切り替え %s: %s", symbol, message)
    threading.Thread(target=run, daemon=True).start()
