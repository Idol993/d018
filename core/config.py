"""
配置管理模块
负责加载、验证和统一访问系统YAML配置
"""
import os
import copy
import logging
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "configs",
    "settings.yaml",
)


class ConfigError(Exception):
    """配置相关异常"""
    pass


class Config:
    """配置管理器 - 线程安全的单例模式"""

    _instance: Optional["Config"] = None
    _config_data: Dict[str, Any] = {}
    _loaded: bool = False

    def __new__(cls, config_path: Optional[str] = None) -> "Config":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: Optional[str] = None) -> None:
        if not self._loaded:
            self._config_path = config_path or _DEFAULT_CONFIG_PATH
            self._load()
            self._loaded = True

    def _load(self) -> None:
        """加载并解析YAML配置文件"""
        if not os.path.exists(self._config_path):
            raise ConfigError(f"配置文件不存在: {self._config_path}")

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._config_data = yaml.safe_load(f) or {}
            logger.info("配置文件加载成功: %s", self._config_path)
            self._validate()
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML解析失败: {e}") from e
        except Exception as e:
            raise ConfigError(f"配置加载异常: {e}") from e

    def _validate(self) -> None:
        """校验配置完整性"""
        required_sections = ["system", "pre_check", "approval", "grayscale", "monitor", "notification"]
        for section in required_sections:
            if section not in self._config_data:
                raise ConfigError(f"缺少必需的配置节: {section}")

        grayscale = self._config_data.get("grayscale", {})
        if "release_strategy" not in grayscale or "stages" not in grayscale["release_strategy"]:
            raise ConfigError("灰度发布策略 stages 配置缺失")
        if "circuit_breaker" not in grayscale:
            raise ConfigError("熔断机制 circuit_breaker 配置缺失")

    def get(self, key_path: str, default: Any = None) -> Any:
        """
        通过点分路径获取配置值
        例如: config.get("grayscale.circuit_breaker.jam_rate_threshold")
        """
        keys = key_path.split(".")
        value: Any = self._config_data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def get_section(self, section: str) -> Dict[str, Any]:
        """获取整个配置节的深拷贝"""
        data = self._config_data.get(section, {})
        return copy.deepcopy(data)

    def get_pre_check_thresholds(self) -> Dict[str, float]:
        """获取前置校验阈值"""
        pc = self.get_section("pre_check")
        return {
            "sorting_accuracy": float(pc.get("sorting_accuracy_threshold", 0.995)),
            "belt_availability": float(pc.get("belt_availability_threshold", 0.98)),
            "scanner_success_rate": float(pc.get("scanner_success_rate_threshold", 0.99)),
            "plc_wcs_handshake_check": bool(pc.get("plc_wcs_handshake_check", True)),
            "instruction_set_compatibility_check": bool(
                pc.get("instruction_set_compatibility_check", True)
            ),
            "regression_test_sample_size": int(pc.get("regression_test_sample_size", 1000)),
            "check_timeout": int(pc.get("check_timeout", 300)),
        }

    def get_approval_channel(self, channel: str) -> Dict[str, Any]:
        """获取指定审批通道配置"""
        approval_cfg = self.get_section("approval")
        channels = approval_cfg.get("channels", {})
        if channel not in channels:
            raise ConfigError(f"未知的审批通道: {channel}")
        return copy.deepcopy(channels[channel])

    def get_grayscale_stages(self) -> List[Dict[str, Any]]:
        """获取灰度发布阶段列表"""
        strategy = self.get("grayscale.release_strategy", {})
        return copy.deepcopy(strategy.get("stages", []))

    def get_circuit_breaker_thresholds(self) -> Dict[str, Any]:
        """获取熔断阈值配置"""
        cb = self.get_section("grayscale").get("circuit_breaker", {})
        return {
            "jam_rate_threshold": float(cb.get("jam_rate_threshold", 0.005)),
            "mis_sort_rate_threshold": float(cb.get("mis_sort_rate_threshold", 0.003)),
            "downtime_count_threshold": int(cb.get("downtime_count_threshold", 2)),
            "auto_rollback": bool(cb.get("auto_rollback", True)),
            "restart_monitor_after_rollback": bool(cb.get("restart_monitor_after_rollback", True)),
            "cooldown_minutes": int(cb.get("cooldown_minutes", 30)),
        }

    def get_monitor_interval(self) -> int:
        """获取监控拉取间隔（秒）"""
        return int(self.get("grayscale.monitor_interval_seconds", 300))

    def get_notification_config(self) -> Dict[str, Any]:
        """获取通知配置"""
        return self.get_section("notification")

    def get_default_approvers(self, role: str) -> List[str]:
        """获取指定角色的默认审批人"""
        return list(self.get(f"approval.default_approvers.{role}", []))

    def all(self) -> Dict[str, Any]:
        """获取完整配置的深拷贝"""
        return copy.deepcopy(self._config_data)


def get_config() -> Config:
    """获取全局配置单例"""
    return Config()
