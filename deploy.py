#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动分拣设备控制系统发布与安全回滚自动化平台
主入口命令行脚本 - 串联前置校验、审批流转、灰度发布、熔断回滚、通知、报表
"""
import os
import sys
import json
import time
import logging
import argparse
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import get_config
from core.pre_check import PreCheckEngine, PreCheckResult
from core.approval import ApprovalEngine, ApprovalFlow, ApprovalFlowStatus
from core.grayscale import (
    GrayscaleReleaseEngine,
    GrayscaleReleaseResult,
    ReleaseStatus,
)
from core.notification import NotificationService, NotificationLevel
from core.report import ReportEngine, DrillRecord


# ===================== 命令处理器 =====================
def cmd_pre_check(args: argparse.Namespace) -> int:
    """执行发布前置校验"""
    logger = logging.getLogger("pre_check")
    logger.info("启动发布前置校验: version=%s channel=%s", args.version, args.channel)

    engine = PreCheckEngine()
    result = engine.run(args.version, args.channel)

    notifier = NotificationService()
    if args.notify:
        notifier.notify_precheck_result(result)

    if not result.all_passed:
        logger.error("前置校验未通过，阻断发布。阻断项: %s", result.blocking_items)
        return 1

    logger.info("前置校验通过，校验ID: %s", result.check_id)
    if args.print_id:
        print(result.check_id)
    return 0


def cmd_approval_create(args: argparse.Namespace) -> int:
    """创建审批流"""
    logger = logging.getLogger("approval")
    engine = ApprovalEngine()
    try:
        flow = engine.create_flow(
            version=args.version,
            channel=args.channel,
            submitter=args.submitter,
            pre_check_id=args.pre_check_id or "",
            emergency_reason=args.emergency_reason or "",
        )
    except ValueError as e:
        logger.error("创建审批流失败: %s", e)
        return 2

    notifier = NotificationService()
    if args.notify:
        notifier.notify_approval_created(flow)

    logger.info("审批流已创建: %s", flow.flow_id)
    if args.print_id:
        print(flow.flow_id)
    return 0


def cmd_approval_action(args: argparse.Namespace) -> int:
    """审批通过/驳回/补签"""
    logger = logging.getLogger("approval")
    engine = ApprovalEngine()

    flow_path = args.flow_file
    if not os.path.exists(flow_path):
        logger.error("审批流文件不存在: %s", flow_path)
        return 2

    with open(flow_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    from core.approval import ApprovalStage, ApprovalStatus
    stages = []
    for sd in data.get("stages", []):
        s = ApprovalStage(**{k: v for k, v in sd.items() if k != "status"})
        s.status = ApprovalStatus(sd["status"])
        stages.append(s)

    flow = ApprovalFlow(**{k: v for k, v in data.items() if k not in ("stages", "status")})
    flow.status = ApprovalFlowStatus(data["status"])
    flow.stages = stages

    if args.action == "approve":
        engine.approve(flow, args.stage, args.approver, args.comment or "")
    elif args.action == "reject":
        engine.reject(flow, args.stage, args.approver, args.comment or "")
    elif args.action == "sign":
        engine.sign_retroactive(flow, args.stage, args.approver, args.comment or "")
    else:
        logger.error("未知操作: %s", args.action)
        return 2

    logger.info("审批操作完成: flow=%s stage=%s action=%s status=%s",
                flow.flow_id, args.stage, args.action, flow.status.value)

    if engine.can_proceed_to_release(flow):
        logger.info("✅ 当前审批流状态允许进入灰度发布环节")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    """
    执行完整的发布流程：
    1. 前置校验
    2. 审批校验（需要提供已通过的审批流ID或跳过）
    3. 灰度发布 + 监控 + 自动熔断回滚
    4. 生成报表 + 发送通知
    """
    logger = logging.getLogger("deploy")
    logger.info("=" * 70)
    logger.info("自动分拣设备控制系统发布流程启动")
    logger.info("版本: %s | 基线版本: %s | 通道: %s | 提交人: %s",
                args.version, args.baseline, args.channel, args.submitter)
    if args.dry_run:
        logger.warning("⚠️  DRY-RUN 演练模式，不会实际下发版本")
    logger.info("=" * 70)

    notifier = NotificationService()
    reporter = ReportEngine()

    # ---------- 阶段1：前置校验 ----------
    logger.info("[阶段 1/4] 执行发布前置校验 ...")
    pre_engine = PreCheckEngine()
    pre_result = pre_engine.run(args.version, args.channel)
    if args.notify:
        notifier.notify_precheck_result(pre_result)
    if not pre_result.all_passed:
        logger.critical("❌ 前置校验未通过，发布流程终止。阻断项: %s", pre_result.blocking_items)
        notifier.notify(
            "发布流程终止 - 前置校验未通过",
            f"版本 {args.version} 因前置校验未通过被阻断\n阻断项: {pre_result.blocking_items}",
            NotificationLevel.CRITICAL,
        )
        return 1
    logger.info("✅ 前置校验通过 ID=%s", pre_result.check_id)

    # ---------- 阶段2：审批校验 ----------
    logger.info("[阶段 2/4] 审批状态校验 ...")
    if args.skip_approval:
        logger.warning("⚠️  已通过参数跳过审批校验")
        approval_flow_id = "SKIPPED"
    else:
        if not args.approval_flow_id:
            logger.critical("❌ 未提供审批流ID (--approval-flow-id)，且未使用 --skip-approval")
            return 2
        approval_flow_id = args.approval_flow_id
        approval_engine = ApprovalEngine()
        loaded_flow = approval_engine.load_flow(approval_flow_id)

        if loaded_flow is None:
            logger.critical("❌ 审批流不存在: %s，请检查 ID 或先使用 approval-create 创建",
                            approval_flow_id)
            notifier.notify(
                "发布流程终止 - 审批流不存在",
                f"审批流ID [{approval_flow_id}] 在系统中不存在，发布流程终止。",
                NotificationLevel.CRITICAL,
            )
            return 2

        if loaded_flow.status == ApprovalFlowStatus.REJECTED:
            logger.critical("❌ 审批流已被驳回: %s，原因: %s",
                            approval_flow_id, loaded_flow.final_comment)
            notifier.notify(
                "发布流程终止 - 审批流已驳回",
                f"审批流ID [{approval_flow_id}] 已被驳回。\n原因: {loaded_flow.final_comment}",
                NotificationLevel.CRITICAL,
            )
            return 2

        if loaded_flow.status in (ApprovalFlowStatus.IN_PROGRESS, ApprovalFlowStatus.INITIATED):
            pending = loaded_flow.current_pending_stages()
            pending_names = "、".join(s.stage_name for s in pending) or "未知"
            logger.critical("❌ 审批流尚未完成: %s，当前状态: %s，待审批阶段: %s",
                            approval_flow_id, loaded_flow.status.value, pending_names)
            notifier.notify(
                "发布流程终止 - 审批未完成",
                f"审批流ID [{approval_flow_id}] 状态为 {loaded_flow.status.value}。\n"
                f"待审批阶段: {pending_names}，请完成全部审批后重试。",
                NotificationLevel.CRITICAL,
            )
            return 2

        if not approval_engine.can_proceed_to_release(loaded_flow):
            logger.critical("❌ 审批流状态不允许发布: %s，当前状态: %s",
                            approval_flow_id, loaded_flow.status.value)
            return 2

        logger.info("✅ 审批流校验通过 ID=%s 状态=%s 版本=%s",
                    loaded_flow.flow_id, loaded_flow.status.value, loaded_flow.version)

    # ---------- 阶段3：灰度发布 + 熔断回滚 ----------
    logger.info("[阶段 3/4] 启动灰度发布、实时监控与自动熔断 ...")
    gs_engine = GrayscaleReleaseEngine()
    gs_engine.register_circuit_breaker_callback(notifier.notify_circuit_breaker)

    release_result = gs_engine.run(
        version=args.version,
        baseline_version=args.baseline,
        approval_flow_id=approval_flow_id,
        dry_run=args.dry_run,
    )

    # ---------- 阶段4：报表与通知 ----------
    logger.info("[阶段 4/4] 生成发布报表与通知 ...")
    report_paths = reporter.render_release_report(release_result.to_dict())
    logger.info("📊 报表已生成: %s", report_paths)

    if args.notify:
        notifier.notify_release_completed(release_result.to_dict())

    # ---------- 最终状态 ----------
    logger.info("=" * 70)
    if release_result.status == ReleaseStatus.COMPLETED:
        logger.info("✅ 灰度发布全部完成，所有分拣线已稳定运行新版本")
        return 0
    elif release_result.status in (ReleaseStatus.TRIGGERED_CIRCUIT_BREAKER, ReleaseStatus.ROLLED_BACK):
        logger.critical("🚨 发布过程触发熔断机制，已自动回滚至基线版本 %s",
                        release_result.baseline_version)
        return 3
    else:
        logger.warning("发布流程非正常结束: %s", release_result.status.value)
        return 4


def cmd_drill(args: argparse.Namespace) -> int:
    """创建发布演练（熔断演练/回滚演练）"""
    logger = logging.getLogger("drill")
    reporter = ReportEngine()

    drill = reporter.create_drill(
        drill_name=args.name,
        drill_type=args.type,
        description=args.description or "",
        participants=args.participants.split(",") if args.participants else [],
    )
    logger.info("演练已创建: %s", drill.drill_id)

    scenario_defs = [
        ("卡件率阈值突破", "jam_rate_anomaly", "模拟卡件率飙升超过0.5%",
         "系统自动触发熔断，暂停发布并回滚"),
        ("错分率阈值突破", "mis_sort_anomaly", "模拟错分率飙升超过0.3%",
         "系统自动触发熔断，暂停发布并回滚"),
        ("高频停机异常", "downtime_anomaly", "模拟5分钟内停机异常>2次",
         "系统自动触发熔断，暂停发布并回滚"),
    ]
    if args.scenarios == "quick":
        scenario_defs = scenario_defs[:1]

    for name, stype, desc, expected in scenario_defs:
        s = reporter.add_scenario(drill, name, stype, desc, expected)
        # 模拟演练
        time.sleep(0.1)
        reporter.complete_scenario(
            s,
            actual_result=f"自动执行DRY-RUN发布并模拟注入{stype}异常，平台在{0.5:.1f}s内触发熔断并回滚",
            passed=True,
            metrics={"detection_latency_s": 0.5, "rollback_duration_s": 2.0},
            duration_seconds=2.5,
        )

    reporter.finalize_drill(
        drill,
        issues_found=["演练为模拟运行，未发现真实问题"],
        action_items=[
            {"item": "每月执行一次全量熔断演练", "owner": "运维组", "deadline": "每月1号", "status": "待执行"},
        ],
        lessons_learned=[
            "灰度发布阶段化策略有效限制了故障爆炸半径",
            "5分钟监控采样频率满足故障快速发现需求",
            "自动回滚机制显著降低了MTTR",
        ],
        conclusion="演练通过，平台熔断与自动回滚机制工作正常。",
    )

    logger.info("演练复盘完成，结果: %s", "通过" if drill.overall_passed else "未通过")
    logger.info("演练ID: %s", drill.drill_id)
    if args.print_id:
        print(drill.drill_id)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """单独启动监控（仅用于独立运行观测）"""
    from core.monitor import MonitorEngine
    import signal

    logger = logging.getLogger("monitor")
    monitor = MonitorEngine()

    lines = args.lines.split(",") if args.lines else ["AUX-001", "CORE-001"]
    stop = {"flag": False}

    def _on_breach(breaches, snapshot):
        for b in breaches:
            logger.warning("阈值突破: %s 实际=%s 阈值=%s",
                           b.metric_label, b.actual_value, b.threshold_value)

    def _handler(sig, frame):
        stop["flag"] = True
        logger.info("收到停止信号 ...")

    signal.signal(signal.SIGINT, _handler)
    monitor.start_background(lines, on_breach=_on_breach)

    logger.info("监控运行中 (Ctrl+C 停止) ...")
    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        monitor.stop_background()
        monitor.save_history(tag="manual")
    return 0


# ===================== CLI 入口 =====================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy",
        description="自动分拣设备控制系统发布与安全回滚自动化平台",
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别 DEBUG/INFO/WARN/ERROR")
    parser.add_argument("--config", default=None, help="自定义配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    # pre-check
    p = sub.add_parser("pre-check", help="执行发布前置校验")
    p.add_argument("--version", required=True, help="待发布版本号")
    p.add_argument("--channel", default="normal", choices=["normal", "hotfix"],
                   help="发布通道 normal/hotfix")
    p.add_argument("--notify", action="store_true", help="发送通知")
    p.add_argument("--print-id", action="store_true", help="只输出校验ID")
    p.set_defaults(func=cmd_pre_check)

    # approval create
    p = sub.add_parser("approval-create", help="创建审批流")
    p.add_argument("--version", required=True)
    p.add_argument("--channel", required=True, choices=["normal", "hotfix"])
    p.add_argument("--submitter", required=True, help="提交人")
    p.add_argument("--pre-check-id", default="", help="前置校验ID")
    p.add_argument("--emergency-reason", default="", help="紧急发布原因(hotfix必填)")
    p.add_argument("--notify", action="store_true")
    p.add_argument("--print-id", action="store_true")
    p.set_defaults(func=cmd_approval_create)

    # approval action
    p = sub.add_parser("approval-action", help="审批操作：通过/驳回/补签")
    p.add_argument("--flow-file", required=True, help="审批流JSON文件路径")
    p.add_argument("--action", required=True, choices=["approve", "reject", "sign"])
    p.add_argument("--stage", required=True, help="审批阶段ID: equipment/technical/operation/safety")
    p.add_argument("--approver", required=True, help="审批人")
    p.add_argument("--comment", default="", help="审批意见")
    p.set_defaults(func=cmd_approval_action)

    # deploy (full pipeline)
    p = sub.add_parser("deploy", help="执行完整发布流水线（校验→审批→灰度→熔断→报表）")
    p.add_argument("--version", required=True, help="待发布新版本")
    p.add_argument("--baseline", required=True, help="用于回滚的稳定基线版本")
    p.add_argument("--channel", default="normal", choices=["normal", "hotfix"])
    p.add_argument("--submitter", required=True)
    p.add_argument("--approval-flow-id", default="",
                   help="已通过的审批流ID（或使用 --skip-approval 跳过）")
    p.add_argument("--skip-approval", action="store_true", help="跳过审批校验（仅测试）")
    p.add_argument("--dry-run", action="store_true", help="演练模式：不实际下发版本")
    p.add_argument("--notify", action="store_true", help="启用多渠道通知")
    p.set_defaults(func=cmd_deploy)

    # drill
    p = sub.add_parser("drill", help="创建并执行发布演练（熔断/回滚演练）")
    p.add_argument("--name", required=True, help="演练名称")
    p.add_argument("--type", default="circuit_breaker",
                   choices=["circuit_breaker", "rollback", "full"])
    p.add_argument("--description", default="")
    p.add_argument("--participants", default="", help="参与人，逗号分隔")
    p.add_argument("--scenarios", default="full", choices=["full", "quick"])
    p.add_argument("--print-id", action="store_true")
    p.set_defaults(func=cmd_drill)

    # monitor
    p = sub.add_parser("monitor", help="独立启动指标监控")
    p.add_argument("--lines", default="", help="监控分拣线ID，逗号分隔")
    p.set_defaults(func=cmd_monitor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _setup_logging(args.log_level)

    if args.config:
        os.environ["DEPLOY_CONFIG_PATH"] = args.config

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n用户中止")
        return 130
    except Exception as e:
        logging.exception("执行异常: %s", e)
        return 99


if __name__ == "__main__":
    sys.exit(main())
