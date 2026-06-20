"""
灰度发布、实时监控与自动熔断回滚模块
负责按分拣线阶梯放量发布、5分钟高频监控、指标异常时立即熔断并回滚
"""
import os
import json
import time
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .config import get_config
from .monitor import MonitorEngine, ThresholdBreach, MetricsSnapshot

logger = logging.getLogger(__name__)


class ReleaseStageStatus(str, Enum):
    PENDING = "pending"
    DEPLOYING = "deploying"
    MONITORING = "monitoring"
    STABLE = "stable"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ReleaseStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    TRIGGERED_CIRCUIT_BREAKER = "triggered_circuit_breaker"
    ROLLED_BACK = "rolled_back"


@dataclass
class StageExecution:
    """单阶段灰度执行记录"""
    stage_id: int
    stage_name: str
    line_ids: List[str]
    description: str
    traffic_percentage: int
    stable_monitor_minutes: int
    status: ReleaseStageStatus = ReleaseStageStatus.PENDING
    deploy_started_at: Optional[str] = None
    deploy_finished_at: Optional[str] = None
    monitor_finished_at: Optional[str] = None
    rollback_triggered_at: Optional[str] = None
    rollback_finished_at: Optional[str] = None
    breaches: List[Dict[str, Any]] = field(default_factory=list)
    deploy_logs: List[str] = field(default_factory=list)
    error_msg: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class CircuitBreakerReport:
    """熔断与回滚结构化报告"""
    report_id: str
    version: str
    trigger_time: str
    affected_line_ids: List[str]
    trigger_stage_name: str
    breaches: List[Dict[str, Any]]
    rollback_started: bool
    rollback_completed_at: Optional[str]
    previous_stable_version: str
    monitor_restarted: bool
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GrayscaleReleaseResult:
    """整体灰度发布结果"""
    release_id: str
    version: str
    baseline_version: str
    approval_flow_id: str
    start_time: str
    end_time: Optional[str] = None
    status: ReleaseStatus = ReleaseStatus.NOT_STARTED
    stages: List[StageExecution] = field(default_factory=list)
    circuit_breaker_report: Optional[CircuitBreakerReport] = None
    total_duration_seconds: float = 0.0
    error_msg: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        if self.circuit_breaker_report:
            data["circuit_breaker_report"] = self.circuit_breaker_report.to_dict()
        return data

    def save(self, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, f"release_{self.release_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return file_path


class Deployer:
    """
    分拣线版本部署器
    生产环境应对接真实WCS/PLC下发接口
    """

    def __init__(self, mock: bool = True) -> None:
        self.mock = mock

    def deploy(self, version: str, line_ids: List[str]) -> tuple[bool, List[str]]:
        """向指定分拣线部署新版本"""
        logs = [f"[{datetime.now().isoformat()}] 开始向分拣线 {line_ids} 部署版本 {version}"]
        if self.mock:
            time.sleep(0.5)
            logs.append(f"[{datetime.now().isoformat()}] 版本下发完成")
            logs.append(f"[{datetime.now().isoformat()}] 设备参数加载完成")
            logs.append(f"[{datetime.now().isoformat()}] PLC程序热加载成功")
            logs.append(f"[{datetime.now().isoformat()}] 分拣线服务重启完成")
            return True, logs
        raise NotImplementedError("生产环境需对接WCS版本下发接口")

    def rollback(self, baseline_version: str, line_ids: List[str]) -> tuple[bool, List[str]]:
        """回滚到指定稳定版本"""
        logs = [f"[{datetime.now().isoformat()}] 开始向分拣线 {line_ids} 回滚到版本 {baseline_version}"]
        if self.mock:
            time.sleep(0.3)
            logs.append(f"[{datetime.now().isoformat()}] 旧版本包加载完成")
            logs.append(f"[{datetime.now().isoformat()}] PLC程序回滚成功")
            logs.append(f"[{datetime.now().isoformat()}] 分拣线服务恢复完成")
            return True, logs
        raise NotImplementedError("生产环境需对接WCS版本回滚接口")


class GrayscaleReleaseEngine:
    """灰度发布引擎 - 编排整个发布/监控/熔断/回滚流程"""

    def __init__(
        self,
        deployer: Optional[Deployer] = None,
        monitor: Optional[MonitorEngine] = None,
    ) -> None:
        self.config = get_config()
        self.deployer = deployer or Deployer(mock=True)
        self.monitor = monitor or MonitorEngine()
        self.thresholds = self.config.get_circuit_breaker_thresholds()
        self.stages_cfg = self.config.get_grayscale_stages()
        self.data_dir = os.path.join(
            self.config.get("system.data_dir", "./data"), "deployment_logs"
        )
        self._on_circuit_breaker_callbacks: List[
            Callable[[CircuitBreakerReport], None]
        ] = []

    def register_circuit_breaker_callback(
        self, callback: Callable[[CircuitBreakerReport], None]
    ) -> None:
        """注册熔断触发回调（用于发送通知等）"""
        self._on_circuit_breaker_callbacks.append(callback)

    def run(
        self,
        version: str,
        baseline_version: str,
        approval_flow_id: str,
        dry_run: bool = False,
    ) -> GrayscaleReleaseResult:
        """
        执行完整灰度发布流程
        :param version: 待发布新版本
        :param baseline_version: 回滚用的稳定基线版本
        :param approval_flow_id: 审批流ID
        :param dry_run: 演练模式（不实际下发）
        """
        release_id = f"RL{uuid.uuid4().hex[:12].upper()}"
        start_ts = time.time()
        result = GrayscaleReleaseResult(
            release_id=release_id,
            version=version,
            baseline_version=baseline_version,
            approval_flow_id=approval_flow_id,
            start_time=datetime.now().isoformat(),
            status=ReleaseStatus.IN_PROGRESS,
        )
        logger.info("========== 灰度发布启动 [%s] 目标版本: %s 基线: %s ==========",
                    release_id, version, baseline_version)
        if dry_run:
            logger.warning("当前为 DRY-RUN 演练模式，不会实际执行版本下发")

        stage_execs = [
            StageExecution(
                stage_id=s["stage_id"],
                stage_name=s["name"],
                line_ids=list(s["line_ids"]),
                description=s.get("description", ""),
                traffic_percentage=int(s.get("traffic_percentage", 0)),
                stable_monitor_minutes=int(s.get("stable_monitor_minutes", 15)),
            )
            for s in self.stages_cfg
        ]
        result.stages = stage_execs

        all_deployed_lines: List[str] = []
        circuit_breaker_triggered = False

        for stage in stage_execs:
            if circuit_breaker_triggered:
                stage.status = ReleaseStageStatus.FAILED
                stage.error_msg = "前置阶段触发熔断，本阶段未执行"
                continue

            ok = self._do_deploy_stage(stage, version, dry_run)
            if not ok:
                result.status = ReleaseStatus.PAUSED
                result.error_msg = f"阶段 {stage.stage_name} 部署失败"
                break

            all_deployed_lines.extend(stage.line_ids)

            stable, breaches = self._do_monitor_stage(
                stage, monitor_seconds=self._monitor_seconds_for_dry_run(stage, dry_run)
            )

            if not stable and breaches:
                circuit_breaker_triggered = True
                stage.status = ReleaseStageStatus.FAILED
                stage.breaches = [b.to_dict() for b in breaches]
                stage.rollback_triggered_at = datetime.now().isoformat()
                logger.critical("========== 触发熔断机制！阶段=%s 分拣线=%s ==========",
                                stage.stage_name, stage.line_ids)

                report = self._build_circuit_breaker_report(
                    result, stage, breaches
                )
                result.circuit_breaker_report = report

                rollback_done = False
                if self.thresholds["auto_rollback"]:
                    self._do_rollback(result, stage_execs, baseline_version, dry_run)
                    rollback_done = (result.status == ReleaseStatus.ROLLED_BACK)

                for cb in self._on_circuit_breaker_callbacks:
                    try:
                        cb(report)
                    except Exception:
                        logger.exception("熔断回调执行异常")

                if not rollback_done:
                    result.status = ReleaseStatus.TRIGGERED_CIRCUIT_BREAKER
                break

            stage.status = ReleaseStageStatus.STABLE
            stage.monitor_finished_at = datetime.now().isoformat()
            logger.info("阶段稳定放行: %s", stage.stage_name)

        if not circuit_breaker_triggered and result.status != ReleaseStatus.PAUSED:
            result.status = ReleaseStatus.COMPLETED

        result.end_time = datetime.now().isoformat()
        result.total_duration_seconds = round(time.time() - start_ts, 2)

        if self.monitor.is_running():
            self.monitor.stop_background()

        try:
            self.monitor.save_history(tag=release_id)
            path = result.save(self.data_dir)
            logger.info("灰度发布报告已保存: %s", path)
        except Exception as e:
            logger.error("保存发布报告失败: %s", e)

        self._log_release_summary(result)
        return result

    # ---------- 内部实现 ----------
    def _monitor_seconds_for_dry_run(self, stage: StageExecution, dry_run: bool) -> int:
        if dry_run:
            return min(3, stage.stable_monitor_minutes * 60)
        return stage.stable_monitor_minutes * 60

    def _do_deploy_stage(
        self, stage: StageExecution, version: str, dry_run: bool
    ) -> bool:
        stage.status = ReleaseStageStatus.DEPLOYING
        stage.deploy_started_at = datetime.now().isoformat()
        logger.info("部署阶段 [%s] 分拣线=%s 流量=%d%%",
                    stage.stage_name, stage.line_ids, stage.traffic_percentage)

        if dry_run:
            stage.deploy_logs.append(f"[DRY-RUN] 模拟向 {stage.line_ids} 部署 {version}")
            stage.deploy_finished_at = datetime.now().isoformat()
            stage.status = ReleaseStageStatus.MONITORING
            return True

        ok, logs = self.deployer.deploy(version, stage.line_ids)
        stage.deploy_logs.extend(logs)
        stage.deploy_finished_at = datetime.now().isoformat()
        if not ok:
            stage.status = ReleaseStageStatus.FAILED
            stage.error_msg = "版本部署失败"
            logger.error("阶段部署失败: %s", stage.stage_name)
            return False

        stage.status = ReleaseStageStatus.MONITORING
        return True

    def _do_monitor_stage(
        self, stage: StageExecution, monitor_seconds: int
    ) -> tuple[bool, List[ThresholdBreach]]:
        """
        对已部署分拣线执行稳定观察，返回 (是否稳定, 触发熔断的突破列表)
        使用同步采样 + 循环等待，不依赖后台线程，便于流程控制
        """
        interval = self.monitor.interval
        iterations = max(1, monitor_seconds // max(interval, 1))
        logger.info("开始稳定观察: %s 预期时长=%ds 采样间隔=%ds 采样次数=%d",
                    stage.stage_name, monitor_seconds, interval, iterations)

        for i in range(iterations):
            snapshot, breaches = self.monitor.check_once(stage.line_ids)
            if breaches:
                return False, breaches
            if i < iterations - 1:
                time.sleep(interval if monitor_seconds >= 60 else min(interval, 2))

        return True, []

    def _build_circuit_breaker_report(
        self,
        result: GrayscaleReleaseResult,
        stage: StageExecution,
        breaches: List[ThresholdBreach],
    ) -> CircuitBreakerReport:
        breach_dicts = [b.to_dict() for b in breaches]
        affected_lines = sorted(set(lid for b in breaches for lid in b.line_ids))
        summary_parts = [f"版本 {result.version} 在阶段 [{stage.stage_name}] 触发熔断:"]
        for b in breaches:
            summary_parts.append(
                f"- {b.metric_label} 实际值={b.actual_value} 阈值={b.threshold_value}"
            )
        report = CircuitBreakerReport(
            report_id=f"CB{uuid.uuid4().hex[:10].upper()}",
            version=result.version,
            trigger_time=datetime.now().isoformat(),
            affected_line_ids=affected_lines,
            trigger_stage_name=stage.stage_name,
            breaches=breach_dicts,
            rollback_started=self.thresholds["auto_rollback"],
            rollback_completed_at=None,
            previous_stable_version=result.baseline_version,
            monitor_restarted=False,
            summary=" ".join(summary_parts),
        )
        return report

    def _do_rollback(
        self,
        result: GrayscaleReleaseResult,
        stage_execs: List[StageExecution],
        baseline_version: str,
        dry_run: bool,
    ) -> None:
        """回滚所有已部署分拣线"""
        logger.warning("开始自动回滚到基线版本: %s", baseline_version)
        all_lines: List[str] = []
        for s in stage_execs:
            if s.status in (ReleaseStageStatus.MONITORING, ReleaseStageStatus.STABLE, ReleaseStageStatus.FAILED):
                all_lines.extend(s.line_ids)
        all_lines = sorted(set(all_lines))

        if not all_lines:
            logger.warning("无可回滚的分拣线")
            return

        if dry_run:
            logger.warning("[DRY-RUN] 模拟回滚: 分拣线=%s -> %s", all_lines, baseline_version)
            for s in stage_execs:
                if s.line_ids:
                    s.status = ReleaseStageStatus.ROLLED_BACK
                    s.rollback_finished_at = datetime.now().isoformat()
        else:
            ok, logs = self.deployer.rollback(baseline_version, all_lines)
            for s in stage_execs:
                if set(s.line_ids) & set(all_lines):
                    s.deploy_logs.extend(logs)
                    s.rollback_finished_at = datetime.now().isoformat()
                    s.status = ReleaseStageStatus.ROLLED_BACK if ok else ReleaseStageStatus.FAILED
            if not ok:
                logger.error("自动回滚失败，请人工介入！")

        if result.circuit_breaker_report:
            result.circuit_breaker_report.rollback_completed_at = datetime.now().isoformat()

        if self.thresholds["restart_monitor_after_rollback"]:
            logger.info("回滚完成，重启监控守护基线版本稳定性")
            if not dry_run:
                def _noop(*a, **k):
                    pass
                self.monitor.start_background(all_lines, on_breach=_noop)
            if result.circuit_breaker_report:
                result.circuit_breaker_report.monitor_restarted = True

        if result.circuit_breaker_report:
            result.status = ReleaseStatus.ROLLED_BACK
            result.circuit_breaker_report.summary += (
                f" 已自动回滚至 {baseline_version}，"
                f"冷却期 {self.thresholds['cooldown_minutes']} 分钟内禁止再次发布。"
            )

    def _log_release_summary(self, result: GrayscaleReleaseResult) -> None:
        logger.info("========== 灰度发布完成 [%s] 总耗时: %.2fs ==========",
                    result.release_id, result.total_duration_seconds)
        logger.info("最终状态: %s", result.status.value)
        for s in result.stages:
            logger.info("  [%s] %s 分拣线=%s", s.status.value, s.stage_name, s.line_ids)
        if result.circuit_breaker_report:
            logger.warning("熔断报告: %s", result.circuit_breaker_report.summary)


def run_grayscale_release(
    version: str,
    baseline_version: str,
    approval_flow_id: str,
    dry_run: bool = False,
) -> GrayscaleReleaseResult:
    """便捷函数：执行灰度发布"""
    engine = GrayscaleReleaseEngine()
    return engine.run(version, baseline_version, approval_flow_id, dry_run)
