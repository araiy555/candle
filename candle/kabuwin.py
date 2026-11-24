"""kabuステーションのチャート画面を指定銘柄に切り替える（実験的・Windows専用）。

公式のAPIには画面を操作する機能がないため、Windowsの画面操作機能（UI Automation）で
チャート画面の銘柄コード欄を探して書き換える。誤操作を避けるため、入力先が
「チャート」画面の中の銘柄コード欄だと確認できた場合にだけキーを送る。
注文画面など、それ以外の場所には一切入力しない。
"""
from __future__ import annotations

import os
import subprocess
import sys

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
  if ($depth -gt 6 -or $script:lines -gt 250) { return }
  $script:lines++
  $c = $e.Current
  ('  ' * $depth) + $c.ControlType.ProgrammaticName.Replace('ControlType.', '') + ' name=[' + $c.Name + '] id=[' + $c.AutomationId + '] class=[' + $c.ClassName + '] value=[' + (Val $e) + ']'
  foreach ($k in (Children $e)) { Dump $k ($depth + 1) }
}

# kabuステーション本体と、その中（または独立）のチャート画面を探す
$charts = @(); $main = $null
foreach ($w in (Children $AE::RootElement)) {
  $n = $w.Current.Name
  if ($n -like 'チャート*') { $charts += $w }
  if ($n -like 'kabuステーション*') { $main = $w }
}
if (-not $main -and $charts.Count -eq 0) { 'NG:kabuステーションの画面が見つかりません。起動しているか確認してください。'; exit 2 }
if ($main) {
  $queue = @(@($main, 0))
  while ($queue.Count -gt 0) {
    $item = $queue[0]; $queue = @($queue | Select-Object -Skip 1)
    if ($item[1] -ge 4) { continue }
    foreach ($k in (Children $item[0])) {
      if ($k.Current.Name -like 'チャート*') { $charts += $k } else { $queue += , @($k, ($item[1] + 1)) }
    }
  }
}
if ($charts.Count -eq 0) { 'NG:チャート画面が見つかりません。kabuステーションでチャートを1つ開いておいてください。'; exit 3 }
$chart = $charts[0]

if ($inspect) { $script:lines = 0; Dump $chart 0; exit 0 }

# チャート画面の中の銘柄コード欄（「3436/T」のような値が入った入力欄）を探す
$edit = $null
foreach ($e in $chart.FindAll($scope::Descendants, (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $editType)))) {
  $v = Val $e
  if ($v -match '^[0-9A-Za-z]{4,5}(/[0-9A-Za-z]+)?$') { $edit = $e; break }
}
if (-not $edit) { 'NG:チャート画面の銘柄コード欄が見つかりません。'; exit 4 }

try { (New-Object -ComObject WScript.Shell).AppActivate($main.Current.ProcessId) | Out-Null } catch {}
Start-Sleep -Milliseconds 200
$edit.SetFocus()
Start-Sleep -Milliseconds 200

# 入力先がその銘柄コード欄であることを確認できた場合だけキーを送る
$focused = $AE::FocusedElement
$same = $false
try { $same = [System.Windows.Automation.Automation]::Compare($focused.GetRuntimeId(), $edit.GetRuntimeId()) } catch {}
if (-not $same) { 'NG:銘柄コード欄を選択できなかったため、何も入力しませんでした。'; exit 5 }

$set = $false
try { $p = $null; if ($edit.TryGetCurrentPattern($valuePattern, [ref]$p) -and -not $p.Current.IsReadOnly) { $p.SetValue($code); $set = $true } } catch {}
if ($set) { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') }
else { [System.Windows.Forms.SendKeys]::SendWait('^a' + $code + '{ENTER}') }
'OK:' + $code
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
            capture_output=True, timeout=30,
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
