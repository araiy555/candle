"""サインの通知。Windowsのデスクトップ通知・音・Webhook（Discord/Slack）に対応。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

import requests

log = logging.getLogger(__name__)

# 文面は環境変数で渡す（コマンド文字列に埋め込まない）
_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:CANDLE_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:CANDLE_BODY)) > $null
$n = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($n)
"""


class Notifier:
    def __init__(self, desktop: bool = True, sound: bool = True, webhook_url: str = "",
                 copy_code: bool = True):
        self.desktop, self.sound, self.webhook_url = desktop, sound, webhook_url
        self.copy_code = copy_code

    def send(self, title: str, body: str, symbol: str | None = None) -> None:
        """通知を送る。受信処理を止めないよう別スレッドで行い、失敗しても例外は出さない。

        symbol を渡すと銘柄コードをクリップボードに入れる（kabuステーションに貼り付けて使う）。
        """
        log.info("通知: %s / %s", title, body)
        threading.Thread(target=self._send, args=(title, body, symbol), daemon=True).start()

    def _send(self, title: str, body: str, symbol: str | None) -> None:
        if sys.platform == "win32":
            if self.copy_code and symbol and symbol.isalnum():
                try:
                    subprocess.run(["clip"], input=symbol.encode("ascii"), timeout=5,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception as e:
                    log.debug("銘柄コードのコピーに失敗: %s", e)
            if self.sound:
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                except Exception as e:
                    log.debug("音の再生に失敗: %s", e)
            if self.desktop:
                try:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST],
                        env={**os.environ, "CANDLE_TITLE": title, "CANDLE_BODY": body},
                        timeout=20, capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception as e:
                    log.warning("デスクトップ通知に失敗: %s", e)
        if self.webhook_url:
            try:
                text = f"**{title}**\n{body}"
                # Discordは content、Slackは text を読む
                requests.post(self.webhook_url, json={"content": text, "text": text}, timeout=10)
            except Exception as e:
                log.warning("Webhook通知に失敗: %s", e)
