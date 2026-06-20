"""
实时监控模块
负责灰度发布期间的高频指标拉取(默认每5分钟)与阈值检测
核心指标：卡件率、错分率、停机异常次数
"""
import os
import json
import time
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests

from .config import get_config

logger = logging.getLogger(__name__)


@dataclass
class MetricsSnapshot:
    """单次采集的指标快照"""
    timestamp: str
    line_ids: List[str]
    jam_rate: Optional[float] = None
    mis_sort_rate: Optional[float] = None
    downtime_count: Optional[int] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    fetch_success: bool = True
    error_msg: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ThresholdBreach:
    """阈值突破记录"""
    metric: str
    metric_label: str
    actual_value: Any
    threshold_value: Any
    line_ids: List[str]
    timestamp: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MetricsClient:
    """指标数据客户端 - 对接WCS/PLC监控接口"""

    def __init__(self, mock: bool = True) -> None:
        self.config = get_config()
        self.mock = mock
        endpoints = self.config.get("monitor.metrics_endpoints", {})
        self.endpoints: Dict[str, str] = dict(endpoints)
        self.timeout = int(self.config.get("monitor.request_timeout", 10))
        self.retry_count = int(self.config.get("monitor.retry_count", 3))
        self.retry_interval = int(self.config.get("monitor.retry_interval", 5))

    def _request_with_retry(self, url: str) -> Optional[Dict[str, Any]]:
        for attempt in range(1, self.retry_count + 1):
            try:
                resp = requests.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning("指标请求失败 (第%d次): %s", attempt, e)
                if attempt < self.retry_count:
                    time.sleep(self.retry_interval)
        return None

    def _mock_metrics(self, line_ids: List[str]) -> Dict[str, Any]:
        import random
        anomaly = random.random() < 0.08
        return {
            "jam_rate": round(random.uniform(0.001, 0.008 if anomaly else 0.004), 5),
            "mis_sort_rate": round(random.uniform(0.0005, 0.005 if anomaly else 0.002), 5),
            "downtime_count": random.randint(0, 3 if anomaly else 1),
            "line_ids": line_ids,
            "generated_at": datetime.now().isoformat(),
        }

    def fetch(self, line_ids: List[str]) -> MetricsSnapshot:
        """采集指定分拣线的核心业务指标"""
        now = datetime.now().isoformat()
        if self.mock:
            raw = self._mock_metrics(line_ids)
            return MetricsSnapshot(
                timestamp=now,
                line_ids=line_ids,
                jam_rate=raw.get("jam_rate"),
                mis_sort_rate=raw.get("mis_sort_rate"),
                downtime_count=raw.get("downtime_count"),
                raw_response=raw,
                fetch_success=True,
            )

        agg: Dict[str, Any] = {"jam_rate": [], "mis_sort_rate": [], "downtime_count": []}
        errors: List[str] = []

        for key in ["jam_rate", "mis_sort_rate", "downtime_count"]:
            url = self.endpoints.get(key)
            if not url:
                errors.append(f"{key} endpoint未配置")
                continue
            data = self._request_with_retry(url)
            if data is None:
                errors.append(f"{key} 请求失败")
                continue
            value = data.get("value")
            if value is None:
                errors.append(f"{key} 返回数据无 value 字段")
                continue
            agg[key].append(value)

        success = len(errors) == 0
        snapshot = MetricsSnapshot(
            timestamp=now,
            line_ids=line_ids,
            fetch_success=success,
            error_msg="; ".join(errors),
            raw_response=agg,
        )
        if success:
            snapshot.jam_rate = sum(agg["jam_rate"]) / len(agg["jam_rate"]) if agg["jam_rate"] else None
            snapshot.mis_sort_rate = sum(agg["mis_sort_rate"]) / len(agg["mis_sort_rate"]) if agg["mis_sort_rate"] else None
            snapshot.downtime_count = sum(agg["downtime_count"]) if agg["downtime_count"] else None
        return snapshot


class MonitorEngine:
    """监控引擎 - 负责周期采样与阈值检测"""

    METRIC_LABELS = {
        "jam_rate": "卡件率",
        "mis_sort_rate": "错分率",
        "downtime_count": "停机异常次数",
    }

    def __init__(self, metrics_client: Optional[MetricsClient] = None) -> None:
        self.config = get_config()
        self.client = metrics_client or MetricsClient(mock=True)
        self.thresholds = self.config.get_circuit_breaker_thresholds()
        self.interval = self.config.get_monitor_interval()
        self.history: List[MetricsSnapshot] = []
        self.breaches: List[ThresholdBreach] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._on_breach_callbacks: List[Callable[[List[ThresholdBreach], MetricsSnapshot], None]] = []
        self._data_dir = os.path.join(
            self.config.get("system.data_dir", "./data"), "deployment_logs"
        )

    # ---------- 同步检测（单次） ----------
    def check_once(self, line_ids: List[str]) -> tuple[MetricsSnapshot, List[ThresholdBreach]]:
        """执行一次指标采集与阈值检测"""
        snapshot = self.client.fetch(line_ids)
        self.history.append(snapshot)
        breaches = self._detect_breaches(snapshot)
        self.breaches.extend(breaches)
        self._log_snapshot(snapshot, breaches)
        return snapshot, breaches

    # ---------- 异步监控（后台线程） ----------
    def start_background(
        self,
        line_ids: List[str],
        on_breach: Optional[Callable[[List[ThresholdBreach], MetricsSnapshot], None]] = None,
    ) -> None:
        """启动后台监控线程"""
        if self._running:
            logger.warning("监控线程已在运行，忽略重复启动")
            return
        if on_breach:
            self._on_breach_callbacks.append(on_breach)
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, args=(line_ids,), daemon=True, name="MonitorThread"
        )
        self._thread.start()
        logger.info("后台监控已启动，采样间隔=%ds，监控分拣线=%s", self.interval, line_ids)

    def stop_background(self) -> None:
        """停止后台监控"""
        if not self._running:
            return
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval + 5)
        self._running = False
        self._on_breach_callbacks.clear()
        logger.info("后台监控已停止，累计采样=%d次，阈值突破=%d次",
                    len(self.history), len(self.breaches))

    def is_running(self) -> bool:
        return self._running

    # ---------- 内部方法 ----------
    def _monitor_loop(self, line_ids: List[str]) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot, breaches = self.check_once(line_ids)
                if breaches:
                    for cb in self._on_breach_callbacks:
                        try:
                            cb(breaches, snapshot)
                        except Exception:
                            logger.exception("监控回调异常")
            except Exception:
                logger.exception("监控循环异常")
            self._stop_event.wait(self.interval)

    def _detect_breaches(self, snapshot: MetricsSnapshot) -> List[ThresholdBreach]:
        breaches: List[ThresholdBreach] = []
        if not snapshot.fetch_success:
            logger.warning("指标采集失败: %s", snapshot.error_msg)
            return breaches

        checks = [
            ("jam_rate", snapshot.jam_rate, self.thresholds["jam_rate_threshold"],
             lambda a, t: a >= t, f"超过安全阈值 {self.thresholds['jam_rate_threshold'] * 100:.2f}%"),
            ("mis_sort_rate", snapshot.mis_sort_rate, self.thresholds["mis_sort_rate_threshold"],
             lambda a, t: a >= t, f"超过安全阈值 {self.thresholds['mis_sort_rate_threshold'] * 100:.3f}%"),
            ("downtime_count", snapshot.downtime_count, self.thresholds["downtime_count_threshold"],
             lambda a, t: a >= t, f"超过安全阈值 {self.thresholds['downtime_count_threshold']}次"),
        ]

        for metric, actual, threshold, cmp_fn, desc in checks:
            if actual is None:
                continue
            if cmp_fn(actual, threshold):
                breaches.append(ThresholdBreach(
                    metric=metric,
                    metric_label=self.METRIC_LABELS.get(metric, metric),
                    actual_value=actual,
                    threshold_value=threshold,
                    line_ids=snapshot.line_ids,
                    timestamp=snapshot.timestamp,
                    description=desc,
                ))
        return breaches

    def _log_snapshot(self, snapshot: MetricsSnapshot, breaches: List[ThresholdBreach]) -> None:
        if snapshot.fetch_success:
            jr = f"{snapshot.jam_rate * 100:.3f}%" if snapshot.jam_rate is not None else "N/A"
            msr = f"{snapshot.mis_sort_rate * 100:.3f}%" if snapshot.mis_sort_rate is not None else "N/A"
            dc = snapshot.downtime_count if snapshot.downtime_count is not None else "N/A"
            logger.info("[监控采样] %s 卡件率=%s 错分率=%s 停机=%s次 阈值突破=%d",
                        snapshot.timestamp, jr, msr, dc, len(breaches))
        else:
            logger.error("[监控采样失败] %s 原因: %s", snapshot.timestamp, snapshot.error_msg)

        if breaches:
            for b in breaches:
                logger.warning("  [阈值突破] %s: 实际=%s 阈值=%s - %s",
                               b.metric_label, b.actual_value, b.threshold_value, b.description)

    def save_history(self, tag: str = "") -> str:
        """保存监控历史到文件"""
        os.makedirs(self._data_dir, exist_ok=True)
        fname = f"monitor_{tag or datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        path = os.path.join(self._data_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated_at": datetime.now().isoformat(),
                    "interval_seconds": self.interval,
                    "thresholds": self.thresholds,
                    "snapshots": [s.to_dict() for s in self.history],
                    "breaches": [b.to_dict() for b in self.breaches],
                },
                f, ensure_ascii=False, indent=2,
            )
        logger.info("监控历史已保存: %s", path)
        return path
