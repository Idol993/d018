#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速验证脚本：冒烟测试整个平台的核心功能模块（含修复点覆盖）
运行方式：python quick_test.py
"""
import os
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smoke_test")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_config():
    log.info("▶ 1. 测试配置管理模块 ...")
    from core.config import get_config
    cfg = get_config()
    assert cfg.get("system.name"), "system.name 缺失"
    assert cfg.get_pre_check_thresholds()["sorting_accuracy"] >= 0.99
    stages = cfg.get_grayscale_stages()
    assert len(stages) >= 2, "灰度阶段至少2级"
    cb = cfg.get_circuit_breaker_thresholds()
    assert cb["jam_rate_threshold"] > 0
    log.info("   ✅ 配置模块通过")


def test_pre_check():
    log.info("▶ 2. 测试发布前置校验 ...")
    from core.pre_check import PreCheckEngine

    # 2.1 常规通道
    engine = PreCheckEngine()
    normal_result = engine.run("v2.4.0-test", "normal")
    normal_keys = {r.check_key for r in normal_result.results}
    log.info("   常规通道校验项: %s", normal_keys)

    # 2.2 Hotfix 通道 - 修复点1：必须完整包含全部核心指标
    hotfix_result = engine.run("v2.4.0-hotfix", "hotfix")
    hotfix_keys = {r.check_key for r in hotfix_result.results}
    required = {"sorting_accuracy", "belt_availability", "scanner_success_rate",
                "plc_wcs_handshake", "instruction_set_compatibility"}
    missing = required - hotfix_keys
    assert not missing, f"Hotfix缺少核心校验项: {missing}"
    log.info("   ✅ Hotfix通道校验完整（%d项），与常规一致", len(hotfix_keys))

    # 2.3 修复点4：统计自洽 - 分拣准确率、正确/错分数
    for r in hotfix_result.results:
        if r.check_key == "sorting_accuracy":
            details = r.details
            total = details["total_samples"]
            correct = details["correct_sorted"]
            mis = details["mis_sorted"]
            assert 0 <= correct <= total, f"正确分拣数超范围: {correct}/{total}"
            assert mis >= 0, f"错分数为负: {mis}"
            assert correct + mis == total, f"统计不自洽: correct+mis != total"
            acc_val = float(r.actual_value.strip("%")) / 100.0
            assert 0.0 <= acc_val <= 1.0, f"准确率超范围: {acc_val}"
            log.info("   ✅ 分拣准确率统计自洽: 正确=%d 错分=%d 总计=%d 准确率=%.4f",
                     correct, mis, total, acc_val)
        if r.check_key == "belt_availability":
            d = r.details
            assert 0 <= d["running_lines"] <= d["total_lines"]
            assert d["stopped_lines"] >= 0
            assert d["running_lines"] + d["stopped_lines"] == d["total_lines"]
            log.info("   ✅ 皮带线运行率统计自洽: 运行=%d 停机=%d 总计=%d",
                     d["running_lines"], d["stopped_lines"], d["total_lines"])
        if r.check_key == "scanner_success_rate":
            d = r.details
            assert 0 <= d["success_count"] <= d["scanned_count"]
            assert d["failed_count"] >= 0
            assert d["success_count"] + d["failed_count"] == d["scanned_count"]
            log.info("   ✅ 扫码成功率统计自洽: 成功=%d 失败=%d 总计=%d",
                     d["success_count"], d["failed_count"], d["scanned_count"])

    # 输出每条结果
    for r in hotfix_result.results:
        icon = "✅" if r.passed else "❌"
        log.info("   %s %s: %s (阈值 %s)", icon, r.check_name, r.actual_value, r.threshold_value)
    return hotfix_result


def test_approval():
    log.info("▶ 3. 测试分级审批流转 ...")
    from core.approval import ApprovalEngine, ApprovalFlowStatus

    engine = ApprovalEngine()

    # 3.1 创建双流程
    normal_flow = engine.create_flow(
        version="v2.4.0-test",
        channel="normal",
        submitter="dev@company.com",
    )
    hotfix_flow = engine.create_flow(
        version="v2.4.0-hotfix",
        channel="hotfix",
        submitter="oncall@company.com",
        emergency_reason="交叉带分拣机卡件率异常飙升，需紧急修复",
    )
    log.info("   常规流: %s | Hotfix流: %s", normal_flow.flow_id, hotfix_flow.flow_id)

    # 3.2 修复点2：常规审批顺序 - 尝试直接审批第2阶段应失败
    try:
        engine.approve(normal_flow, "technical", "tech@company.com", "跳级审批测试")
        assert False, "应抛出顺序违规异常"
    except ValueError as e:
        assert "顺序违规" in str(e) or "前序" in str(e), f"错误信息不符: {e}"
        log.info("   ✅ 常规审批顺序校验生效：跳级审批被正确拒绝 -> %s", e)

    # 3.3 正确顺序审批第1阶段应成功
    engine.approve(normal_flow, "equipment", "equip@company.com", "设备检查通过")
    assert normal_flow.get_stage("equipment").status.value == "approved"
    log.info("   ✅ 按正确顺序审批第1阶段通过")

    # 3.4 再次审批已通过阶段应失败
    try:
        engine.approve(normal_flow, "equipment", "equip@company.com", "重复审批")
        assert False, "重复审批应失败"
    except ValueError as e:
        log.info("   ✅ 重复审批被正确拒绝: %s", e)

    # 3.5 修复点3：load_flow 加载不存在的审批流应返回 None
    assert engine.load_flow("NOT-EXIST-12345") is None
    log.info("   ✅ 不存在的审批流正确返回 None")

    # 3.6 load_flow 加载已保存审批流
    loaded = engine.load_flow(normal_flow.flow_id)
    assert loaded is not None and loaded.flow_id == normal_flow.flow_id
    log.info("   ✅ 已保存审批流正确加载: %s", loaded.flow_id)

    # 3.7 审批中状态不能进入发布
    assert not engine.can_proceed_to_release(loaded), "审批中不应允许发布"
    log.info("   ✅ 审批中状态正确禁止发布")

    # 3.8 Hotfix 审批顺序（并行）- 任意顺序应允许
    engine.approve(hotfix_flow, "safety", "safety@company.com", "安全补签")
    log.info("   ✅ Hotfix并行审批不限制顺序")

    log.info("   ✅ 审批流转模块全部修复点通过")


def test_monitor():
    log.info("▶ 4. 测试实时监控模块 ...")
    from core.monitor import MonitorEngine
    monitor = MonitorEngine()
    snapshot, breaches = monitor.check_once(["AUX-001", "CORE-001"])
    log.info("   采样成功: %s | 卡件率: %s | 错分率: %s | 停机: %s | 阈值突破: %d",
             snapshot.fetch_success, snapshot.jam_rate, snapshot.mis_sort_rate,
             snapshot.downtime_count, len(breaches))
    assert snapshot.fetch_success
    log.info("   ✅ 监控模块通过")


def test_grayscale():
    log.info("▶ 5. 测试灰度发布与熔断回滚 (DRY-RUN) ...")
    from core.grayscale import GrayscaleReleaseEngine, ReleaseStatus

    engine = GrayscaleReleaseEngine()

    # 5.1 正常发布
    result = engine.run(
        version="v2.4.0-test",
        baseline_version="v2.3.1",
        approval_flow_id="AF-TEST",
        dry_run=True,
    )
    log.info("   正常发布: ID=%s | 状态=%s | 阶段数=%d",
             result.release_id, result.status.value, len(result.stages))

    # 5.2 修复点5：构造强制熔断场景验证最终状态为 rolled_back
    # 通过一个故意返回超阈值指标的 MonitorEngine 子类来触发熔断
    from core.monitor import MonitorEngine, MetricsClient, ThresholdBreach

    class AlwaysBreachClient(MetricsClient):
        def fetch(self, line_ids):
            snap = super().fetch(line_ids)
            snap.jam_rate = 0.999  # 远超阈值
            snap.mis_sort_rate = 0.999
            snap.downtime_count = 999
            return snap

    bad_monitor = MonitorEngine(metrics_client=AlwaysBreachClient(mock=True))
    engine2 = GrayscaleReleaseEngine(monitor=bad_monitor)
    bad_result = engine2.run(
        version="v9.9.9-bad",
        baseline_version="v2.3.1",
        approval_flow_id="AF-CB-TEST",
        dry_run=True,
    )

    log.info("   强制熔断发布: ID=%s | 最终状态=%s",
             bad_result.release_id, bad_result.status.value)

    assert bad_result.circuit_breaker_report is not None, "熔断报告应存在"
    assert bad_result.status == ReleaseStatus.ROLLED_BACK, (
        f"自动回滚后最终状态应为 rolled_back，实际为 {bad_result.status.value}"
    )
    assert bad_result.circuit_breaker_report.rollback_completed_at is not None
    log.info("   ✅ 熔断后最终状态正确: %s (已自动回滚)", bad_result.status.value)
    log.info("   ✅ 熔断报告回滚完成时间: %s", bad_result.circuit_breaker_report.rollback_completed_at)

    # 5.3 JSON 报表包含 rolled_back
    data = bad_result.to_dict()
    assert data["status"] == "rolled_back", f"JSON报表状态应为 rolled_back: {data['status']}"
    log.info("   ✅ JSON 报表状态正确: %s", data["status"])

    # 5.4 阶段状态包含 ROLLED_BACK
    rolled_stages = [s for s in bad_result.stages if s.status.value == "rolled_back"]
    log.info("   ✅ 已回滚阶段数: %d/%d", len(rolled_stages), len(bad_result.stages))

    return bad_result


def test_notification():
    log.info("▶ 6. 测试通知模块 (构造数据，不实际发送) ...")
    from core.notification import NotificationService
    svc = NotificationService()
    log.info("   已启用的渠道数: %d (配置未填Webhook则不会实际发送)", len(svc.channels))
    log.info("   ✅ 通知模块通过")


def test_report():
    log.info("▶ 7. 测试演练与复盘报表 ...")
    from core.report import ReportEngine
    reporter = ReportEngine()

    # 7.1 演练报表
    drill = reporter.create_drill(
        drill_name="Q2熔断演练",
        drill_type="circuit_breaker",
        description="模拟卡件率阈值突破场景，验证自动熔断与回滚",
        participants=["张三", "李四", "王五"],
    )
    s = reporter.add_scenario(
        drill, "卡件率异常飙升", "jam_rate_anomaly",
        "模拟卡件率达到1.2%", "系统自动熔断并回滚"
    )
    reporter.complete_scenario(
        s, "系统于0.8秒内检测到异常，触发熔断，2.3秒内完成回滚",
        passed=True, metrics={"detection_latency_s": 0.8, "rollback_s": 2.3}, duration_seconds=3.1
    )
    reporter.finalize_drill(
        drill,
        action_items=[{"item": "每月演练一次", "owner": "运维组", "deadline": "每月1号", "status": "待执行"}],
        lessons_learned=["自动熔断有效降低故障影响面"],
        conclusion="演练通过",
    )
    paths = reporter.save_drill(drill)
    log.info("   演练ID: %s | 报表: %s", drill.drill_id, paths)

    # 7.2 修复点5：发布报表（含 rolled_back 状态）HTML 渲染
    from core.grayscale import GrayscaleReleaseEngine, ReleaseStatus
    from core.monitor import MonitorEngine, MetricsClient

    class AlwaysBreachClient2(MetricsClient):
        def fetch(self, line_ids):
            snap = super().fetch(line_ids)
            snap.jam_rate = 0.999
            snap.mis_sort_rate = 0.999
            snap.downtime_count = 999
            return snap

    eng = GrayscaleReleaseEngine(monitor=MonitorEngine(metrics_client=AlwaysBreachClient2(mock=True)))
    bad_result = eng.run(
        version="v9.9.9", baseline_version="v1.0.0",
        approval_flow_id="AF-TEST", dry_run=True,
    )
    report_paths = reporter.render_release_report(bad_result.to_dict())
    log.info("   发布报表（含熔断回滚）: %s", report_paths)

    # 7.3 检查 HTML 中是否包含 rolled_back 状态文本
    html_path = [p for p in report_paths if p.endswith(".html")][0]
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    assert "已回滚" in html, "HTML报表应包含'已回滚'徽标文本"
    log.info("   ✅ HTML 报表包含正确的 '已回滚' 状态徽标")

    for p in report_paths:
        assert os.path.exists(p)
    log.info("   ✅ 报表模块通过")


def main():
    log.info("=" * 60)
    log.info("自动分拣设备发布与回滚平台 - 冒烟测试（含修复点验证）")
    log.info("=" * 60)
    tests = [
        test_config,
        test_pre_check,
        test_approval,
        test_monitor,
        test_grayscale,
        test_notification,
        test_report,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            log.exception("   ❌ %s 测试失败: %s", t.__name__, e)
            failed += 1

    log.info("=" * 60)
    log.info("测试结果: 通过 %d / 失败 %d / 总计 %d", passed, failed, len(tests))
    log.info("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
