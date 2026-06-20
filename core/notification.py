"""
通知模块
支持企业微信、钉钉、邮件多渠道结构化通知
用于发布事件、审批提醒、熔断告警等场景
"""
import json
import time
import hmac
import base64
import hashlib
import logging
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from typing import Any, Dict, List, Optional

import requests

from .config import get_config
from .grayscale import CircuitBreakerReport
from .pre_check import PreCheckResult
from .approval import ApprovalFlow

logger = logging.getLogger(__name__)


class NotificationLevel(str):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class NotificationChannel:
    """通知渠道基类"""

    name: str = "base"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.enabled = bool(config.get("enabled", False))
        self.config = config

    def send(self, title: str, content: str, level: str = NotificationLevel.INFO) -> bool:
        raise NotImplementedError


class WeComChannel(NotificationChannel):
    """企业微信群机器人通知"""

    name = "wecom"

    def send(self, title: str, content: str, level: str = NotificationLevel.INFO) -> bool:
        if not self.enabled:
            return False
        try:
            webhook = self.config.get("webhook_url", "")
            if not webhook:
                logger.warning("企业微信 webhook_url 未配置")
                return False

            color_map = {
                NotificationLevel.INFO: "info",
                NotificationLevel.WARNING: "warning",
                NotificationLevel.CRITICAL: "comment",
            }

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"## {title}\n\n{content}",
                },
            }
            mentioned = self.config.get("mentioned_mobile_list", [])
            if mentioned and level in (NotificationLevel.WARNING, NotificationLevel.CRITICAL):
                payload["markdown"]["content"] += (
                    f"\n\n<@{'>@<@'.join(mentioned)}>"
                )

            resp = requests.post(webhook, json=payload, timeout=10)
            ok = resp.status_code == 200 and resp.json().get("errcode", -1) == 0
            if not ok:
                logger.warning("企业微信发送失败: %s", resp.text)
            return ok
        except Exception as e:
            logger.exception("企业微信通知异常: %s", e)
            return False


class DingTalkChannel(NotificationChannel):
    """钉钉群机器人通知（支持加签）"""

    name = "dingtalk"

    def _sign(self, timestamp: int, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return urllib.parse.quote_plus(base64.b64encode(hmac_code))

    def send(self, title: str, content: str, level: str = NotificationLevel.INFO) -> bool:
        if not self.enabled:
            return False
        try:
            webhook = self.config.get("webhook_url", "")
            if not webhook:
                logger.warning("钉钉 webhook_url 未配置")
                return False

            secret = self.config.get("secret", "")
            if secret:
                ts = round(time.time() * 1000)
                sign = self._sign(ts, secret)
                sep = "&" if "?" in webhook else "?"
                webhook = f"{webhook}{sep}timestamp={ts}&sign={sign}"

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": title,
                    "text": f"## {title}\n\n{content}",
                },
                "at": {
                    "atMobiles": self.config.get("at_mobiles", []),
                    "isAtAll": level == NotificationLevel.CRITICAL,
                },
            }

            resp = requests.post(webhook, json=payload, timeout=10)
            ok = resp.status_code == 200 and resp.json().get("errcode", -1) == 0
            if not ok:
                logger.warning("钉钉发送失败: %s", resp.text)
            return ok
        except Exception as e:
            logger.exception("钉钉通知异常: %s", e)
            return False


class EmailChannel(NotificationChannel):
    """SMTP 邮件通知"""

    name = "email"

    def send(self, title: str, content: str, level: str = NotificationLevel.INFO) -> bool:
        if not self.enabled:
            return False
        try:
            host = self.config.get("smtp_host", "")
            port = int(self.config.get("smtp_port", 465))
            use_ssl = bool(self.config.get("use_ssl", True))
            sender = self.config.get("sender", "")
            password = self.config.get("password", "")
            receivers = list(self.config.get("receivers", []))

            if not all([host, sender, password, receivers]):
                logger.warning("邮件配置不完整")
                return False

            level_prefix = {
                NotificationLevel.INFO: "[INFO]",
                NotificationLevel.WARNING: "[WARN]",
                NotificationLevel.CRITICAL: "[CRITICAL]",
            }.get(level, "")

            msg = MIMEMultipart("alternative")
            msg["From"] = Header(sender)
            msg["To"] = Header(", ".join(receivers))
            msg["Subject"] = Header(f"{level_prefix} {title}", "utf-8")

            html_content = f"""
            <html><body style="font-family:Arial,sans-serif;">
            <h2 style="color:#333;">{title}</h2>
            <div style="white-space:pre-wrap;color:#444;line-height:1.6;">{content}</div>
            <hr style="margin-top:20px;border:none;border-top:1px solid #eee;"/>
            <p style="color:#999;font-size:12px;">由自动分拣设备发布平台自动发送</p>
            </body></html>
            """
            msg.attach(MIMEText(content, "plain", "utf-8"))
            msg.attach(MIMEText(html_content, "html", "utf-8"))

            if use_ssl:
                server = smtplib.SMTP_SSL(host, port, timeout=15)
            else:
                server = smtplib.SMTP(host, port, timeout=15)
                server.starttls()

            try:
                server.login(sender, password)
                server.sendmail(sender, receivers, msg.as_string())
            finally:
                server.quit()
            return True
        except Exception as e:
            logger.exception("邮件通知异常: %s", e)
            return False


class NotificationService:
    """通知服务 - 统一多渠道分发入口"""

    _LEVEL_EMOJI = {
        NotificationLevel.INFO: "ℹ️",
        NotificationLevel.WARNING: "⚠️",
        NotificationLevel.CRITICAL: "🚨",
    }

    def __init__(self) -> None:
        self.config = get_config()
        notif_cfg = self.config.get_notification_config()
        self.channels: List[NotificationChannel] = []

        if notif_cfg.get("wecom", {}).get("enabled"):
            self.channels.append(WeComChannel(notif_cfg["wecom"]))
        if notif_cfg.get("dingtalk", {}).get("enabled"):
            self.channels.append(DingTalkChannel(notif_cfg["dingtalk"]))
        if notif_cfg.get("email", {}).get("enabled"):
            self.channels.append(EmailChannel(notif_cfg["email"]))

        if not self.channels:
            logger.warning("未启用任何通知渠道，所有通知将仅输出到日志")

    def notify(self, title: str, content: str, level: str = NotificationLevel.INFO) -> bool:
        """统一发送通知到所有已启用渠道"""
        emoji = self._LEVEL_EMOJI.get(level, "")
        full_title = f"{emoji} {title}" if emoji else title
        logger.info("[通知:%s] %s - %s", level, full_title, content[:200])

        if not self.channels:
            return True

        success = False
        for ch in self.channels:
            try:
                if ch.send(full_title, content, level):
                    success = True
            except Exception as e:
                logger.exception("通知渠道 %s 异常: %s", ch.name, e)
        return success

    # ---------- 业务场景快捷方法 ----------
    def notify_precheck_result(self, result: PreCheckResult) -> None:
        level = NotificationLevel.INFO if result.all_passed else NotificationLevel.WARNING
        title = f"发布前置校验 {'通过' if result.all_passed else '未通过'}"
        lines = [
            f"- **校验ID**: {result.check_id}",
            f"- **版本**: {result.version}",
            f"- **耗时**: {result.duration_seconds}s",
            f"- **结果摘要**: {result.summary}",
        ]
        for item in result.results:
            icon = "✅" if item.passed else "❌"
            lines.append(
                f"- {icon} **{item.check_name}**: {item.actual_value} (阈值 {item.threshold_value})"
            )
            if item.suggestion and not item.passed:
                lines.append(f"  > 修复建议: {item.suggestion}")
        self.notify(title, "\n".join(lines), level)

    def notify_approval_created(self, flow: ApprovalFlow) -> None:
        title = f"发布审批已创建 - {flow.channel_name}"
        lines = [
            f"- **审批流ID**: {flow.flow_id}",
            f"- **版本**: {flow.version}",
            f"- **提交人**: {flow.submitter}",
            f"- **模式**: {'并行审批' if flow.parallel else '串行审批'}",
            f"- **状态**: {flow.status.value}",
        ]
        if flow.emergency_reason:
            lines.append(f"- **紧急原因**: {flow.emergency_reason}")
        lines.append("\n**审批矩阵**:")
        for s in flow.stages:
            lines.append(
                f"- [{s.stage_order}] {s.stage_name} ({s.role}) - "
                f"审批人: {', '.join(s.approvers) or '未配置'} / 时限 {s.timeout_hours}h"
            )
        self.notify(title, "\n".join(lines), NotificationLevel.INFO)

    def notify_circuit_breaker(self, report: CircuitBreakerReport) -> None:
        lines = [
            f"**熔断报告ID**: {report.report_id}",
            f"**触发时间**: {report.trigger_time}",
            f"**问题版本**: {report.version}",
            f"**触发阶段**: {report.trigger_stage_name}",
            f"**影响分拣线**: {', '.join(report.affected_line_ids)}",
            f"**回滚基线版本**: {report.previous_stable_version}",
            f"**已自动回滚**: {'是' if report.rollback_started else '否'}",
            f"**监控已重启**: {'是' if report.monitor_restarted else '否'}",
            "\n**阈值突破明细**:",
        ]
        for b in report.breaches:
            lines.append(
                f"- ❌ **{b['metric_label']}**: 实际 {b['actual_value']} / 阈值 {b['threshold_value']} - {b['description']}"
            )
        lines.append(f"\n> {report.summary}")
        self.notify("🚨 灰度发布触发熔断并自动回滚", "\n".join(lines), NotificationLevel.CRITICAL)

    def notify_release_completed(self, release_result: Dict[str, Any]) -> None:
        status = release_result.get("status", "unknown")
        level = NotificationLevel.INFO if status in ("completed",) else NotificationLevel.WARNING
        title = f"灰度发布流程结束 - {status}"
        lines = [
            f"- **发布ID**: {release_result.get('release_id')}",
            f"- **版本**: {release_result.get('version')}",
            f"- **基线版本**: {release_result.get('baseline_version')}",
            f"- **总耗时**: {release_result.get('total_duration_seconds')}s",
            f"- **最终状态**: {status}",
        ]
        stages = release_result.get("stages", [])
        if stages:
            lines.append("\n**阶段执行情况**:")
            for s in stages:
                icon = "✅" if s.get("status") == "stable" else ("🔄" if s.get("status") == "rolled_back" else "❌")
                lines.append(f"- {icon} {s.get('stage_name')} ({s.get('status')}) - 分拣线: {s.get('line_ids')}")
        self.notify(title, "\n".join(lines), level)


def get_notifier() -> NotificationService:
    """获取全局通知服务实例"""
    return NotificationService()
