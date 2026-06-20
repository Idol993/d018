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
    # 先删除冷却期文件，确保不会被前面的测试影响
    if os.path.exists(eng._cooldown_path):
        os.remove(eng._cooldown_path)
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


def test_hotfix_release_guard():
    log.info("▶ 8. 测试 Hotfix 审批发布门槛（真正通过才放行）+ approval-status 摘要 ...")
    from core.approval import ApprovalEngine, ApprovalFlowStatus

    engine = ApprovalEngine()

    # 8.1 创建 Hotfix 审批流
    hotfix_flow = engine.create_flow(
        version="9.9.9-hotfix",
        channel="hotfix",
        submitter="qa-lead",
        pre_check_id="PC-HOTFIX-001",
        emergency_reason="生产错分率飙升需紧急发布",
    )
    # 初始创建后还没开始审批 -> 不可发布
    assert engine.can_proceed_to_release(hotfix_flow) is False, (
        "刚创建的hotfix流不应直接允许发布"
    )
    log.info("   ✅ 刚创建 Hotfix 流: 不可发布")

    # 8.2 让其中2个阶段通过，另外2个保持 pending
    engine.approve(hotfix_flow, "equipment", "device-mgr", "设备侧无风险")
    engine.approve(hotfix_flow, "technical", "tech-lead", "技术逻辑OK")
    # 状态应为 WAITING_RETROACTIVE（并行审批中，仍有未完成）
    after_two = engine.check_timeout(hotfix_flow)
    assert engine.can_proceed_to_release(after_two) is False, (
        "待补签状态的 hotfix 流不应允许发布"
    )
    log.info("   ✅ 待补签状态: 不可发布 (status=%s)", after_two.status.value)

    # 8.3 用 approval-status 摘要验证状态描述
    summary = engine.get_approval_status_summary(after_two)
    assert summary["overall"]["status"] in (
        "in_progress", "waiting_retroactive"
    ), f"状态应为 in_progress 或 waiting_retroactive，得到 {summary['overall']['status']}"
    assert summary["can_proceed"] is False
    pending_names = [s["stage_name"] for s in summary["stages"] if s["status"] == "pending"]
    assert len(pending_names) == 2, f"应剩2个待处理阶段，实际 {pending_names}"
    assert summary["next_step"] and summary["next_step"].get("parallel_count") == 2, (
        "Hotfix 应提示可并行处理剩余阶段"
    )
    log.info("   ✅ approval-status 摘要: 包含 %s 待补签阶段，下一步处理并行数量=%d",
             pending_names, summary["next_step"]["parallel_count"])

    # 8.4 全部补签完成 -> 真正 APPROVED -> 可发布
    engine.sign_retroactive(after_two, "operation", "ops-mgr", "运营侧事后复核通过")
    engine.sign_retroactive(after_two, "safety", "sec-mgr", "安全侧事后复核通过")
    final = engine.check_timeout(after_two)
    assert engine.can_proceed_to_release(final), (
        "全部补签完成后 hotfix 流必须允许发布"
    )
    summary2 = engine.get_approval_status_summary(final)
    assert summary2["can_proceed"] is True
    assert "全部审批已通过" in summary2["next_step"]["message"]
    log.info("   ✅ 全部补签完成: 状态=%s 下一步提示=%s，可发布",
             final.status.value, summary2["next_step"]["message"])


def test_cooldown():
    log.info("▶ 9. 测试灰度发布冷却期机制 ...")
    import os, json, time, tempfile
    from datetime import datetime, timedelta
    from core.grayscale import GrayscaleReleaseEngine, ReleaseStatus
    from core.monitor import MonitorEngine, MetricsClient, ThresholdBreach, MetricsSnapshot

    # 定义一个始终触发熔断的 Mock Client（本地类，供冷却期测试使用）
    class AlwaysBreachClient(MetricsClient):
        def _real_check(self, line_ids, thresholds):
            return [ThresholdBreach(
                metric_key="jam_rate", metric_label="卡件率",
                actual_value=0.015, threshold_value=thresholds["jam_rate_threshold"],
                line_ids=line_ids, timestamp=datetime.now().isoformat(),
            )], MetricsSnapshot(
                timestamp=datetime.now().isoformat(),
                jam_rate=0.015, mis_sort_rate=0.0, downtime_count=0, line_ids=list(line_ids),
            )

    # 强制构造熔断+回滚场景，生成冷却期状态
    engine = GrayscaleReleaseEngine()
    # 先清除已有冷却期文件，避免测试被干扰
    if os.path.exists(engine._cooldown_path):
        os.remove(engine._cooldown_path)

    # 9.1 初始状态：无冷却期
    ok, info, msg = engine.check_cooldown_before_release()
    assert ok and info is None, "初始状态应无冷却期"
    log.info("   ✅ 初始状态: 无冷却期，允许发布")

    # 9.2 强制构造冷却期状态文件（模拟最近一次刚刚熔断回滚）
    fake_state = {
        "report_id": "CB-FAKE-001",
        "triggered_version": "2.3.4",
        "trigger_time": (datetime.now() - timedelta(minutes=10)).isoformat(),
        "rollback_completed_at": (datetime.now() - timedelta(minutes=5)).isoformat(),
        "cooldown_minutes": 30,
        "cooldown_until": (datetime.now() + timedelta(minutes=25)).isoformat(),
        "previous_stable_version": "2.3.0",
        "trigger_stage": "核心全流量",
        "affected_line_ids": ["CORE-001", "CORE-002"],
        "breaches": [
            {"metric_label": "卡件率", "actual_value": 0.012, "threshold_value": 0.005}
        ],
    }
    os.makedirs(os.path.dirname(engine._cooldown_path), exist_ok=True)
    with open(engine._cooldown_path, "w", encoding="utf-8") as f:
        json.dump(fake_state, f)

    # 9.3 冷却期内：应被拒绝
    ok, info, msg = engine.check_cooldown_before_release()
    assert ok is False and info is not None, "冷却期内应拒绝发布"
    assert info["triggered_version"] == "2.3.4"
    assert "核心全流量" in msg, "提示里应包含触发阶段"
    assert "CORE-001, CORE-002" in msg, "提示里应包含影响分拣线"
    assert info["remaining_minutes"] > 0, "应存在剩余冷却时间"
    log.info("   ✅ 冷却期内: 发布被拒绝 | 触发版本=%s | 剩余冷却=%.2fmin",
             info["triggered_version"], info["remaining_minutes"])

    # 9.4 真正调用 run() 应直接返回 PAUSED
    mon = MonitorEngine()
    mon.client = AlwaysBreachClient()  # 实际不会用到，因为冷却期先返回
    engine2 = GrayscaleReleaseEngine(monitor=mon)
    # 复制一份冷却期文件到新引擎目录（应该同一个）
    res = engine2.run("9.9.9-new", "9.9.0-baseline", "AP-COOLDOWN-TEST", dry_run=True)
    assert res.status == ReleaseStatus.PAUSED, (
        f"冷却期内应返回 PAUSED，实际={res.status.value}"
    )
    assert "熔断冷却期" in res.error_msg, "error_msg 应包含冷却期提示"
    log.info("   ✅ 灰度引擎 run() 在冷却期内返回 PAUSED，error_msg=%s",
             res.error_msg[:80] + "...")

    # 9.5 修改状态为冷却已到期，应恢复允许发布
    expired_state = dict(fake_state)
    expired_state["cooldown_until"] = (datetime.now() - timedelta(minutes=1)).isoformat()
    with open(engine._cooldown_path, "w", encoding="utf-8") as f:
        json.dump(expired_state, f)
    ok, info, msg = engine.check_cooldown_before_release()
    assert ok and info is None, "冷却期到期后应允许发布"
    log.info("   ✅ 冷却期过期后: 恢复允许发布")
    os.remove(engine._cooldown_path)


def test_precheck_report_and_status():
    log.info("▶ 10. 测试前置校验报告（最近一次+指定ID）+ approval-status 完整输出 ...")
    import os, json, tempfile
    from core.pre_check import PreCheckEngine, PreCheckResult

    # 10.1 先做一次前置校验生成报告
    pc_engine = PreCheckEngine()
    result = pc_engine.run("5.6.7-test", channel="hotfix")
    assert len(result.results) == 5, "Hotfix 应执行完整 5 项校验"
    # 10.2 load_latest 能读到
    latest = pc_engine.load_latest()
    assert latest is not None and latest.check_id == result.check_id, (
        "load_latest 应读到刚生成的报告"
    )
    log.info("   ✅ load_latest 能正确返回最近一次报告: %s", latest.check_id)
    # 10.3 load_result 指定 ID
    by_id = pc_engine.load_result(result.check_id)
    assert by_id is not None and by_id.version == "5.6.7-test"
    log.info("   ✅ load_result 按 ID 读取正确: version=%s", by_id.version)

    # 10.4 不存在的 ID
    assert pc_engine.load_result("NOT-EXIST-ID-12345") is None, "不存在 ID 应返回 None"
    log.info("   ✅ 不存在 ID 返回 None")

    # 10.5 build_terminal_summary 内容自洽
    summary_text = pc_engine.build_terminal_summary(result)
    assert "核心指标明细" in summary_text
    assert result.check_id in summary_text
    # 5 项指标都应该出现
    for name in ["分拣准确率", "皮带线运行率", "扫码器识别", "PLC-WCS握手", "指令集兼容"]:
        assert name in summary_text, f"终端摘要应包含 {name}"
    log.info("   ✅ build_terminal_summary 包含全部 5 项指标名称")

    # 10.6 测试 report.render_pre_check_report 生成 JSON+HTML
    from core.report import ReportEngine
    reporter = ReportEngine()
    paths = reporter.render_pre_check_report(result.to_dict())
    assert len(paths) >= 1
    json_paths = [p for p in paths if p.endswith(".json")]
    html_paths = [p for p in paths if p.endswith(".html")]
    assert json_paths, "应生成 JSON 报告"
    with open(json_paths[0], "r", encoding="utf-8") as f:
        jr = json.load(f)
    assert jr["check_id"] == result.check_id
    assert len(jr["results"]) == 5
    log.info("   ✅ render_pre_check_report JSON 报告生成成功，包含 %d 项校验", len(jr["results"]))
    if html_paths:
        with open(html_paths[0], "r", encoding="utf-8") as f:
            html_text = f.read()
        assert "样本总数" in html_text or "校验通过" in html_text
        log.info("   ✅ render_pre_check_report HTML 报告渲染成功 (%s)", html_paths[0])

    # 10.7 approval-status 四阶段完整输出（常规审批+跳级场景）
    from core.approval import ApprovalEngine
    ae = ApprovalEngine()
    flow = ae.create_flow(
        version="5.6.7", channel="normal", submitter="dev-a", pre_check_id=result.check_id
    )
    summary = ae.get_approval_status_summary(flow)
    stages = summary["stages"]
    assert len(stages) == 4
    assert [s["stage_order"] for s in stages] == [1, 2, 3, 4]
    assert summary["overall"]["status"] == "in_progress"
    assert summary["next_step"]["stage_order"] == 1
    assert summary["next_step"]["stage_id"] == "equipment"
    log.info("   ✅ approval-status 四阶段顺序校验: 下一步=%s(第%d级)",
             summary["next_step"]["stage_name"], summary["next_step"]["stage_order"])


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
        test_hotfix_release_guard,
        test_cooldown,
        test_precheck_report_and_status,
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
