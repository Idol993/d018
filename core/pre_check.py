"""
发布前置校验模块
负责开发提交发布申请后自动触发的多维质量门禁检查
校验维度：分拣准确率、皮带线运行率、扫码器识别成功率、设备联动校验
"""
import os
import json
import time
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import get_config

logger = logging.getLogger(__name__)


@dataclass
class CheckItemResult:
    """单条校验项结果"""
    check_name: str
    check_key: str
    passed: bool
    actual_value: Any
    threshold_value: Any
    description: str
    suggestion: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PreCheckResult:
    """整体前置校验结果"""
    check_id: str
    version: str
    start_time: str
    end_time: str = ""
    duration_seconds: float = 0.0
    all_passed: bool = False
    results: List[CheckItemResult] = field(default_factory=list)
    blocking_items: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data

    def save(self, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, f"precheck_{self.check_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return file_path


class MetricsProvider:
    """
    指标数据提供者
    生产环境应对接真实WCS/PLC数据接口，此处提供可插拔的抽象层
    """

    def __init__(self, mock: bool = True) -> None:
        self.mock = mock

    def fetch_sorting_accuracy(self, sample_size: int) -> Tuple[float, Dict[str, Any]]:
        """获取分拣准确率 (0~1) - 保证统计自洽"""
        if self.mock:
            import random
            raw_rate = 0.993 + random.uniform(-0.01, 0.015)
            clamped_rate = min(max(raw_rate, 0.0), 1.0)
            correct = int(sample_size * clamped_rate)
            correct = min(correct, sample_size)
            mis = sample_size - correct
            mis = max(mis, 0)
            actual_acc = correct / sample_size if sample_size > 0 else 0.0
            details = {
                "total_samples": sample_size,
                "correct_sorted": correct,
                "mis_sorted": mis,
                "test_dataset": "historical_parcels_2026Q1",
            }
            assert 0.0 <= actual_acc <= 1.0, "分拣准确率必须在 [0, 1] 区间"
            assert correct <= sample_size, "正确分拣数不能大于总样本数"
            assert mis >= 0, "错分数不能为负数"
            assert correct + mis == sample_size, "正确+错分必须等于总样本数"
            return round(actual_acc, 6), details
        raise NotImplementedError("生产环境需对接WCS历史数据接口")

    def fetch_belt_availability(self) -> Tuple[float, Dict[str, Any]]:
        """获取皮带线运行率 (0~1) - 保证统计自洽"""
        if self.mock:
            import random
            total_lines = 8
            raw_rate = 0.975 + random.uniform(-0.02, 0.03)
            clamped_rate = min(max(raw_rate, 0.0), 1.0)
            running = int(total_lines * clamped_rate)
            running = min(max(running, 0), total_lines)
            actual_rate = running / total_lines
            details = {
                "total_lines": total_lines,
                "running_lines": running,
                "stopped_lines": total_lines - running,
                "measurement_window_hours": 24,
                "downtime_records": [],
            }
            assert 0.0 <= actual_rate <= 1.0
            assert 0 <= running <= total_lines
            return round(actual_rate, 6), details
        raise NotImplementedError("生产环境需对接SCADA运行数据")

    def fetch_scanner_success_rate(self) -> Tuple[float, Dict[str, Any]]:
        """获取扫码器识别成功率 (0~1) - 保证统计自洽"""
        if self.mock:
            import random
            scanned_count = 50000
            raw_rate = 0.988 + random.uniform(-0.015, 0.015)
            clamped_rate = min(max(raw_rate, 0.0), 1.0)
            success = int(scanned_count * clamped_rate)
            success = min(max(success, 0), scanned_count)
            failed = scanned_count - success
            failed = max(failed, 0)
            actual_rate = success / scanned_count
            details = {
                "total_scanners": 24,
                "scanned_count": scanned_count,
                "success_count": success,
                "failed_count": failed,
                "failed_barcode_types": ["CODE128_damaged", "QR_folded"],
            }
            assert 0.0 <= actual_rate <= 1.0
            assert 0 <= success <= scanned_count
            assert failed >= 0
            assert success + failed == scanned_count
            return round(actual_rate, 6), details
        raise NotImplementedError("生产环境需对接扫码器集中管理接口")

    def check_plc_wcs_handshake(self) -> Tuple[bool, Dict[str, Any]]:
        """PLC与WCS系统握手信号校验"""
        if self.mock:
            import random
            ok = random.random() > 0.05
            details = {
                "plc_ip_list": ["192.168.1.10", "192.168.1.11"],
                "wcs_endpoint": "http://wcs.internal/api/v1/plc/handshake",
                "handshake_protocol": "OPC-UA",
                "last_heartbeat": datetime.now().isoformat(),
            }
            return ok, details
        raise NotImplementedError("生产环境需对接OPC-UA握手测试")

    def check_instruction_set_compatibility(self, version: str) -> Tuple[bool, Dict[str, Any]]:
        """指令集兼容性校验"""
        if self.mock:
            import random
            ok = random.random() > 0.03
            details = {
                "target_version": version,
                "baseline_version": "v2.3.1",
                "supported_instructions": 156,
                "deprecated_instructions": 0,
                "breaking_changes": [],
            }
            return ok, details
        raise NotImplementedError("生产环境需对接指令集兼容性矩阵")


class PreCheckEngine:
    """前置校验引擎"""

    def __init__(self, metrics_provider: Optional[MetricsProvider] = None) -> None:
        self.config = get_config()
        self.thresholds = self.config.get_pre_check_thresholds()
        self.metrics = metrics_provider or MetricsProvider(mock=True)
        self.data_dir = os.path.join(self.config.get("system.data_dir", "./data"), "deployment_logs")

    def run(self, version: str, channel: str = "normal") -> PreCheckResult:
        """
        执行完整的前置校验流程
        :param version: 待发布版本号
        :param channel: 发布通道 normal/hotfix
        :return: PreCheckResult
        """
        check_id = f"PC{uuid.uuid4().hex[:12].upper()}"
        start_ts = time.time()
        start_time = datetime.now().isoformat()
        logger.info("========== 开始发布前置校验 [%s] 版本: %s 通道: %s ==========",
                    check_id, version, channel)

        result = PreCheckResult(
            check_id=check_id,
            version=version,
            start_time=start_time,
        )

        check_runners: List[Callable[[], CheckItemResult]] = [
            self._check_sorting_accuracy,
            self._check_belt_availability,
            self._check_scanner_success_rate,
        ]
        if self.thresholds["plc_wcs_handshake_check"]:
            check_runners.append(self._check_plc_wcs_handshake)
        if self.thresholds["instruction_set_compatibility_check"]:
            check_runners.append(lambda: self._check_instruction_set_compatibility(version))

        if channel == "hotfix":
            logger.info("Hotfix通道：质量门禁范围与常规发布一致（仅审批流程并行化）")

        for runner in check_runners:
            try:
                item = runner()
                result.results.append(item)
                if not item.passed:
                    result.blocking_items.append(item.check_key)
                    logger.warning("校验未通过: %s 实际值: %s 阈值: %s",
                                   item.check_name, item.actual_value, item.threshold_value)
            except Exception as e:
                logger.exception("校验异常: %s", e)
                result.results.append(CheckItemResult(
                    check_name="校验异常",
                    check_key="check_exception",
                    passed=False,
                    actual_value=str(e),
                    threshold_value="-",
                    description="校验过程发生异常",
                    suggestion="请联系平台管理员排查",
                ))
                result.blocking_items.append("check_exception")

        result.end_time = datetime.now().isoformat()
        result.duration_seconds = round(time.time() - start_ts, 2)
        result.all_passed = len(result.blocking_items) == 0
        result.summary = self._build_summary(result)

        self._persist_result(result)
        self._log_summary(result)
        return result

    # ---------- 单项校验实现 ----------
    def _check_sorting_accuracy(self) -> CheckItemResult:
        threshold = self.thresholds["sorting_accuracy"]
        sample_size = self.thresholds["regression_test_sample_size"]
        actual, details = self.metrics.fetch_sorting_accuracy(sample_size)
        passed = actual >= threshold
        suggestion = "" if passed else (
            f"分拣准确率低于阈值。建议：1) 检查图像识别模型参数；"
            f"2) 复现 {details.get('mis_sorted', 0)} 件错分包裹场景并修复；"
            f"3) 使用更大样本集重新回归测试。"
        )
        return CheckItemResult(
            check_name="分拣准确率回归测试",
            check_key="sorting_accuracy",
            passed=passed,
            actual_value=f"{actual * 100:.2f}%",
            threshold_value=f"{threshold * 100:.2f}%",
            description=f"基于 {sample_size} 件历史包裹数据的回归测试准确率",
            suggestion=suggestion,
            details=details,
        )

    def _check_belt_availability(self) -> CheckItemResult:
        threshold = self.thresholds["belt_availability"]
        actual, details = self.metrics.fetch_belt_availability()
        passed = actual >= threshold
        suggestion = "" if passed else (
            f"皮带线运行率低于阈值。建议：1) 检查 {details.get('total_lines', 0) - details.get('running_lines', 0)} 条停机皮带的机械/电气故障；"
            f"2) 确认维护计划是否已完成；3) 评估是否具备发布条件。"
        )
        return CheckItemResult(
            check_name="皮带线运行率",
            check_key="belt_availability",
            passed=passed,
            actual_value=f"{actual * 100:.2f}%",
            threshold_value=f"{threshold * 100:.2f}%",
            description="近24小时全部皮带线运行率",
            suggestion=suggestion,
            details=details,
        )

    def _check_scanner_success_rate(self) -> CheckItemResult:
        threshold = self.thresholds["scanner_success_rate"]
        actual, details = self.metrics.fetch_scanner_success_rate()
        passed = actual >= threshold
        suggestion = "" if passed else (
            f"扫码器识别成功率低于阈值。建议：1) 检查清洁镜头；"
            f"2) 针对失败条码类型 {details.get('failed_barcode_types', [])} 优化识别算法；"
            f"3) 更换老化扫码设备。"
        )
        return CheckItemResult(
            check_name="扫码器识别成功率",
            check_key="scanner_success_rate",
            passed=passed,
            actual_value=f"{actual * 100:.2f}%",
            threshold_value=f"{threshold * 100:.2f}%",
            description="近24小时全部扫码器综合识别成功率",
            suggestion=suggestion,
            details=details,
        )

    def _check_plc_wcs_handshake(self) -> CheckItemResult:
        actual, details = self.metrics.check_plc_wcs_handshake()
        suggestion = "" if actual else (
            "PLC与WCS握手失败。建议：1) 检查OPC-UA连接状态；"
            "2) 核对PLC IP及端口；3) 确认WCS服务健康状态。"
        )
        return CheckItemResult(
            check_name="PLC-WCS握手信号校验",
            check_key="plc_wcs_handshake",
            passed=bool(actual),
            actual_value="SUCCESS" if actual else "FAILED",
            threshold_value="SUCCESS",
            description="PLC控制器与WCS控制系统间的握手信号与通信状态",
            suggestion=suggestion,
            details=details,
        )

    def _check_instruction_set_compatibility(self, version: str) -> CheckItemResult:
        actual, details = self.metrics.check_instruction_set_compatibility(version)
        suggestion = "" if actual else (
            f"指令集存在不兼容变更。建议：1) 审核破坏性变更 {details.get('breaking_changes', [])}；"
            f"2) 更新PLC程序以适配新指令集；3) 提供兼容层避免破坏性升级。"
        )
        return CheckItemResult(
            check_name="指令集兼容性校验",
            check_key="instruction_set_compatibility",
            passed=bool(actual),
            actual_value="COMPATIBLE" if actual else "INCOMPATIBLE",
            threshold_value="COMPATIBLE",
            description=f"待发布版本 {version} 与基线版本的指令集兼容性",
            suggestion=suggestion,
            details=details,
        )

    # ---------- 内部工具 ----------
    def _build_summary(self, result: PreCheckResult) -> str:
        total = len(result.results)
        passed_count = sum(1 for r in result.results if r.passed)
        if result.all_passed:
            return f"全部 {total} 项校验通过，准予进入审批环节。"
        return (
            f"共 {total} 项校验，{passed_count} 项通过，"
            f"{len(result.blocking_items)} 项未通过（阻断发布）。"
            f"阻断项：{', '.join(result.blocking_items)}"
        )

    def _persist_result(self, result: PreCheckResult) -> None:
        try:
            path = result.save(self.data_dir)
            logger.info("前置校验报告已保存: %s", path)
        except Exception as e:
            logger.error("保存校验报告失败: %s", e)

    def _log_summary(self, result: PreCheckResult) -> None:
        logger.info("========== 前置校验完成 [%s] 耗时: %.2fs ==========",
                    result.check_id, result.duration_seconds)
        logger.info("结果: %s", "通过" if result.all_passed else "阻断")
        logger.info("摘要: %s", result.summary)
        for item in result.results:
            status = "✓" if item.passed else "✗"
            logger.info("  %s [%s] %s: 实际=%s 阈值=%s",
                        status, item.check_key, item.check_name,
                        item.actual_value, item.threshold_value)


def run_pre_check(version: str, channel: str = "normal") -> PreCheckResult:
    """便捷函数：执行前置校验"""
    engine = PreCheckEngine()
    return engine.run(version, channel)
