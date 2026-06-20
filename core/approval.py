"""
分级审批流转模块
支持常规迭代(四级串行审批)与紧急热修复(并行/事后补签)双通道
动态审批矩阵：设备 -> 技术 -> 运营 -> 安全
"""
import os
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import get_config

logger = logging.getLogger(__name__)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    RETROACTIVE = "retroactive"


class ApprovalFlowStatus(str, Enum):
    INITIATED = "initiated"
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    WAITING_RETROACTIVE = "waiting_retroactive"


@dataclass
class ApprovalStage:
    """单级审批节点"""
    stage_id: str
    stage_order: int
    stage_name: str
    role: str
    description: str
    approvers: List[str]
    status: ApprovalStatus = ApprovalStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    comment: str = ""
    timeout_hours: int = 24
    deadline: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class ApprovalFlow:
    """完整审批流"""
    flow_id: str
    version: str
    channel: str
    channel_name: str
    submitter: str
    submit_time: str
    emergency_reason: str = ""
    pre_check_id: str = ""
    status: ApprovalFlowStatus = ApprovalFlowStatus.INITIATED
    stages: List[ApprovalStage] = field(default_factory=list)
    parallel: bool = False
    allow_retroactive: bool = False
    completed_time: Optional[str] = None
    final_comment: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "flow_id": self.flow_id,
            "version": self.version,
            "channel": self.channel,
            "channel_name": self.channel_name,
            "submitter": self.submitter,
            "submit_time": self.submit_time,
            "emergency_reason": self.emergency_reason,
            "pre_check_id": self.pre_check_id,
            "status": self.status.value,
            "stages": [s.to_dict() for s in self.stages],
            "parallel": self.parallel,
            "allow_retroactive": self.allow_retroactive,
            "completed_time": self.completed_time,
            "final_comment": self.final_comment,
        }
        return data

    def save(self, output_dir: str) -> str:
        os.makedirs(output_dir, exist_ok=True)
        file_path = os.path.join(output_dir, f"approval_{self.flow_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        return file_path

    def get_stage(self, stage_id: str) -> Optional[ApprovalStage]:
        for s in self.stages:
            if s.stage_id == stage_id:
                return s
        return None

    def current_pending_stages(self) -> List[ApprovalStage]:
        if self.parallel:
            return [s for s in self.stages if s.status == ApprovalStatus.PENDING]
        for s in sorted(self.stages, key=lambda x: x.stage_order):
            if s.status == ApprovalStatus.PENDING:
                return [s]
            if s.status in (ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT):
                return []
        return []


class ApprovalEngine:
    """审批流引擎"""

    VALID_CHANNELS = ("normal", "hotfix")

    def __init__(self) -> None:
        self.config = get_config()
        self.data_dir = os.path.join(
            self.config.get("system.data_dir", "./data"), "approval_records"
        )

    def load_flow(self, flow_id: str) -> Optional[ApprovalFlow]:
        """
        从磁盘加载审批流
        :return: ApprovalFlow 或 None（不存在时）
        """
        path = os.path.join(self.data_dir, f"approval_{flow_id}.json")
        if not os.path.exists(path):
            logger.warning("审批流文件不存在: %s", path)
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stages = []
            for sd in data.get("stages", []):
                s = ApprovalStage(**{k: v for k, v in sd.items() if k != "status"})
                s.status = ApprovalStatus(sd["status"])
                stages.append(s)
            flow = ApprovalFlow(
                **{k: v for k, v in data.items() if k not in ("stages", "status")}
            )
            flow.status = ApprovalFlowStatus(data["status"])
            flow.stages = stages
            return flow
        except Exception as e:
            logger.error("加载审批流失败 %s: %s", flow_id, e)
            return None

    def load_flow_from_path(self, file_path: str) -> Optional[ApprovalFlow]:
        """从任意指定文件路径加载审批流（与 load_flow 逻辑一致，便于外部传 flow-file）"""
        if not os.path.exists(file_path):
            logger.warning("审批流文件不存在: %s", file_path)
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stages = []
            for sd in data.get("stages", []):
                s = ApprovalStage(**{k: v for k, v in sd.items() if k != "status"})
                s.status = ApprovalStatus(sd["status"])
                stages.append(s)
            flow = ApprovalFlow(
                **{k: v for k, v in data.items() if k not in ("stages", "status")}
            )
            flow.status = ApprovalFlowStatus(data["status"])
            flow.stages = stages
            return flow
        except Exception as e:
            logger.error("从路径加载审批流失败 %s: %s", file_path, e)
            return None

    def validate_stage_order(self, flow: ApprovalFlow, stage_id: str) -> tuple[bool, str]:
        """
        校验审批阶段顺序（仅常规串行审批需要严格顺序）
        :return: (是否合法, 错误信息/当前可处理阶段名)
        """
        # Hotfix 并行审批：不强制顺序
        if flow.parallel:
            return True, ""

        # 串行审批：必须按 stage_order 从小到大依次处理
        sorted_stages = sorted(flow.stages, key=lambda x: x.stage_order)
        target = flow.get_stage(stage_id)
        if target is None:
            return False, f"阶段 [{stage_id}] 不存在"

        # 检查是否有更早的阶段还没通过
        for s in sorted_stages:
            if s.stage_order < target.stage_order:
                if s.status not in (ApprovalStatus.APPROVED, ApprovalStatus.RETROACTIVE):
                    prev_name = next(
                        (x.stage_name for x in sorted_stages
                         if x.stage_order == s.stage_order), s.stage_id
                    )
                    return (
                        False,
                        f"需先完成前序阶段 [{prev_name}（{s.status.value}）]，"
                        f"当前只能处理第{s.stage_order}级审批，"
                        f"阶段 [{target.stage_name}] 为第{target.stage_order}级，暂不可审批。"
                    )
            elif s.stage_order == target.stage_order:
                if s.status != ApprovalStatus.PENDING:
                    return False, f"阶段 [{target.stage_name}] 当前状态为 {s.status.value}，无法再审批"
                return True, ""
        return False, f"阶段 [{stage_id}] 不在审批矩阵中"

    def create_flow(
        self,
        version: str,
        channel: str,
        submitter: str,
        pre_check_id: str = "",
        emergency_reason: str = "",
        approvers_override: Optional[Dict[str, List[str]]] = None,
    ) -> ApprovalFlow:
        """
        创建审批流
        :param version: 发布版本号
        :param channel: 发布通道 normal/hotfix
        :param submitter: 提交人
        :param pre_check_id: 前置校验ID
        :param emergency_reason: 紧急发布原因(hotfix必填)
        :param approvers_override: 审批人覆盖 {role: [emails]}
        """
        if channel not in self.VALID_CHANNELS:
            raise ValueError(f"无效的发布通道: {channel}，可选值: {self.VALID_CHANNELS}")

        channel_cfg = self.config.get_approval_channel(channel)

        if channel == "hotfix" and not emergency_reason.strip():
            raise ValueError("紧急热修复(hotfix)必须填写紧急原因 emergency_reason")

        flow_id = f"AF{uuid.uuid4().hex[:12].upper()}"
        now = datetime.now()

        stages: List[ApprovalStage] = []
        for idx, stage_cfg in enumerate(channel_cfg.get("approvers", [])):
            role = stage_cfg["role"]
            if approvers_override and role in approvers_override:
                approvers = approvers_override[role]
            else:
                approvers = self.config.get_default_approvers(role)

            timeout = int(stage_cfg.get("timeout_hours", 24))
            stage = ApprovalStage(
                stage_id=stage_cfg["stage"],
                stage_order=idx + 1,
                stage_name=stage_cfg["name"],
                role=role,
                description=stage_cfg.get("description", ""),
                approvers=approvers,
                timeout_hours=timeout,
                deadline=(now + timedelta(hours=timeout)).isoformat(),
            )
            stages.append(stage)

        flow = ApprovalFlow(
            flow_id=flow_id,
            version=version,
            channel=channel,
            channel_name=channel_cfg.get("name", channel),
            submitter=submitter,
            submit_time=now.isoformat(),
            emergency_reason=emergency_reason,
            pre_check_id=pre_check_id,
            status=ApprovalFlowStatus.INITIATED,
            stages=stages,
            parallel=bool(channel_cfg.get("parallel", False)),
            allow_retroactive=bool(channel_cfg.get("retroactive_signature", False)),
        )

        if channel == "hotfix":
            flow.status = ApprovalFlowStatus.WAITING_RETROACTIVE
            logger.warning("Hotfix通道已创建 [%s]，可先行发布，事后补签审批", flow_id)
        else:
            flow.status = ApprovalFlowStatus.IN_PROGRESS

        self._persist_flow(flow)
        self._log_flow_created(flow)
        return flow

    def approve(
        self,
        flow: ApprovalFlow,
        stage_id: str,
        approver: str,
        comment: str = "",
    ) -> ApprovalFlow:
        """审批通过"""
        stage = flow.get_stage(stage_id)
        if stage is None:
            raise ValueError(f"审批阶段不存在: {stage_id}")

        ok, msg = self.validate_stage_order(flow, stage_id)
        if not ok:
            raise ValueError(f"审批顺序违规: {msg}")

        if stage.status != ApprovalStatus.PENDING:
            raise ValueError(f"审批阶段状态异常: {stage.status.value}，无法审批")

        stage.status = ApprovalStatus.APPROVED
        stage.approved_by = approver
        stage.approved_at = datetime.now().isoformat()
        stage.comment = comment

        self._evaluate_flow_status(flow)
        self._persist_flow(flow)
        logger.info("审批通过 [%s] 阶段=%s 审批人=%s", flow.flow_id, stage_id, approver)
        return flow

    def reject(
        self,
        flow: ApprovalFlow,
        stage_id: str,
        approver: str,
        comment: str,
    ) -> ApprovalFlow:
        """审批驳回"""
        stage = flow.get_stage(stage_id)
        if stage is None:
            raise ValueError(f"审批阶段不存在: {stage_id}")

        ok, msg = self.validate_stage_order(flow, stage_id)
        if not ok:
            raise ValueError(f"审批顺序违规: {msg}")

        stage.status = ApprovalStatus.REJECTED
        stage.approved_by = approver
        stage.approved_at = datetime.now().isoformat()
        stage.comment = comment

        flow.status = ApprovalFlowStatus.REJECTED
        flow.completed_time = datetime.now().isoformat()
        flow.final_comment = f"被 {approver} 驳回: {comment}"

        self._persist_flow(flow)
        logger.warning("审批驳回 [%s] 阶段=%s 审批人=%s 原因=%s",
                       flow.flow_id, stage_id, approver, comment)
        return flow

    def sign_retroactive(
        self,
        flow: ApprovalFlow,
        stage_id: str,
        approver: str,
        comment: str = "",
    ) -> ApprovalFlow:
        """事后补签（仅hotfix通道）"""
        if not flow.allow_retroactive:
            raise ValueError("当前通道不支持事后补签")

        stage = flow.get_stage(stage_id)
        if stage is None:
            raise ValueError(f"审批阶段不存在: {stage_id}")

        ok, msg = self.validate_stage_order(flow, stage_id)
        if not ok:
            raise ValueError(f"补签顺序违规: {msg}")

        if stage.status != ApprovalStatus.PENDING:
            raise ValueError(f"当前阶段状态为 {stage.status.value}，无需补签")

        stage.status = ApprovalStatus.RETROACTIVE
        stage.approved_by = approver
        stage.approved_at = datetime.now().isoformat()
        stage.comment = f"[事后补签] {comment}"

        self._evaluate_flow_status(flow)
        self._persist_flow(flow)
        logger.info("事后补签完成 [%s] 阶段=%s 补签人=%s", flow.flow_id, stage_id, approver)
        return flow

    def check_timeout(self, flow: ApprovalFlow) -> ApprovalFlow:
        """检查审批超时"""
        now = datetime.now()
        changed = False
        for stage in flow.stages:
            if stage.status == ApprovalStatus.PENDING and stage.deadline:
                deadline = datetime.fromisoformat(stage.deadline)
                if now > deadline:
                    stage.status = ApprovalStatus.TIMEOUT
                    stage.comment = "审批超时自动标记"
                    changed = True
                    logger.warning("审批超时 [%s] 阶段=%s", flow.flow_id, stage.stage_id)

        if changed:
            self._evaluate_flow_status(flow)
            self._persist_flow(flow)
        return flow

    def can_proceed_to_release(self, flow: ApprovalFlow) -> bool:
        """判断是否可以进入发布环节 - 仅审批状态真正为 APPROVED 才允许"""
        return flow.status == ApprovalFlowStatus.APPROVED

    def get_approval_status_summary(self, flow: ApprovalFlow) -> Dict[str, Any]:
        """
        获取审批流完整状态摘要（用于 approval-status 命令展示）
        包含：四阶段详情、超时状态、当前下一步处理人
        """
        self.check_timeout(flow)
        now = datetime.now()
        stages_info = []
        sorted_stages = sorted(flow.stages, key=lambda x: x.stage_order)

        for s in sorted_stages:
            timeout_flag = False
            remaining_hours = None
            if s.deadline:
                deadline = datetime.fromisoformat(s.deadline)
                if s.status == ApprovalStatus.PENDING and now > deadline:
                    timeout_flag = True
                elif s.status == ApprovalStatus.PENDING:
                    delta = deadline - now
                    remaining_hours = round(delta.total_seconds() / 3600, 2)

            stages_info.append({
                "stage_order": s.stage_order,
                "stage_id": s.stage_id,
                "stage_name": s.stage_name,
                "role": s.role,
                "description": s.description,
                "approvers": list(s.approvers),
                "status": s.status.value,
                "status_label": {
                    "pending": "待审批",
                    "approved": "已通过",
                    "rejected": "已驳回",
                    "timeout": "已超时",
                    "retroactive": "事后补签",
                }.get(s.status.value, s.status.value),
                "approved_by": s.approved_by,
                "approved_at": s.approved_at,
                "deadline": s.deadline,
                "timeout_hours": s.timeout_hours,
                "remaining_hours": remaining_hours,
                "is_timeout": timeout_flag,
                "comment": s.comment,
            })

        pending_stages = [s for s in sorted_stages if s.status == ApprovalStatus.PENDING]
        if flow.parallel:
            next_handlers = pending_stages
        else:
            next_handlers = pending_stages[:1] if pending_stages else []

        next_step = None
        if next_handlers:
            next_step = {
                "stage_order": next_handlers[0].stage_order,
                "stage_id": next_handlers[0].stage_id,
                "stage_name": next_handlers[0].stage_name,
                "approvers": list(next_handlers[0].approvers),
                "parallel_count": len(next_handlers),
            }
        elif flow.status == ApprovalFlowStatus.APPROVED:
            next_step = {"message": "全部审批已通过，可进入灰度发布"}
        elif flow.status == ApprovalFlowStatus.REJECTED:
            next_step = {"message": f"审批已终止：{flow.final_comment}"}

        overall = {
            "flow_id": flow.flow_id,
            "version": flow.version,
            "channel": flow.channel,
            "channel_name": flow.channel_name,
            "submitter": flow.submitter,
            "submit_time": flow.submit_time,
            "emergency_reason": flow.emergency_reason,
            "pre_check_id": flow.pre_check_id,
            "status": flow.status.value,
            "status_label": {
                "initiated": "已创建",
                "in_progress": "审批中",
                "approved": "已通过",
                "rejected": "已驳回",
                "waiting_retroactive": "待事后补签",
            }.get(flow.status.value, flow.status.value),
            "completed_time": flow.completed_time,
            "final_comment": flow.final_comment,
            "parallel": flow.parallel,
            "allow_retroactive": flow.allow_retroactive,
        }

        return {
            "overall": overall,
            "stages": stages_info,
            "next_step": next_step,
            "can_proceed": self.can_proceed_to_release(flow),
        }

    # ---------- 内部方法 ----------
    def _evaluate_flow_status(self, flow: ApprovalFlow) -> None:
        if any(s.status == ApprovalStatus.REJECTED for s in flow.stages):
            flow.status = ApprovalFlowStatus.REJECTED
            flow.completed_time = datetime.now().isoformat()
            return
        if any(s.status == ApprovalStatus.TIMEOUT for s in flow.stages):
            flow.status = ApprovalFlowStatus.REJECTED
            flow.completed_time = datetime.now().isoformat()
            flow.final_comment = "审批超时，流程自动终止"
            return

        all_done = all(
            s.status in (ApprovalStatus.APPROVED, ApprovalStatus.RETROACTIVE)
            for s in flow.stages
        )
        if all_done:
            flow.status = ApprovalFlowStatus.APPROVED
            flow.completed_time = datetime.now().isoformat()
            if not flow.final_comment:
                flow.final_comment = "全部审批通过"
        else:
            if flow.channel == "hotfix":
                flow.status = ApprovalFlowStatus.WAITING_RETROACTIVE
            else:
                flow.status = ApprovalFlowStatus.IN_PROGRESS

    def _persist_flow(self, flow: ApprovalFlow) -> None:
        try:
            path = flow.save(self.data_dir)
            logger.debug("审批流已保存: %s", path)
        except Exception as e:
            logger.error("保存审批流失败: %s", e)

    def _log_flow_created(self, flow: ApprovalFlow) -> None:
        logger.info("========== 审批流已创建 [%s] ==========", flow.flow_id)
        logger.info("版本: %s", flow.version)
        logger.info("通道: %s (%s)", flow.channel_name, flow.channel)
        logger.info("提交人: %s", flow.submitter)
        if flow.emergency_reason:
            logger.info("紧急原因: %s", flow.emergency_reason)
        logger.info("审批模式: %s", "并行审批" if flow.parallel else "串行审批")
        logger.info("审批矩阵:")
        for s in flow.stages:
            logger.info("  [%d] %s (%s) -> 审批人: %s 时限: %dh",
                        s.stage_order, s.stage_name, s.role,
                        ", ".join(s.approvers) or "(未配置)", s.timeout_hours)


def create_approval_flow(
    version: str,
    channel: str,
    submitter: str,
    pre_check_id: str = "",
    emergency_reason: str = "",
) -> ApprovalFlow:
    """便捷函数：创建审批流"""
    engine = ApprovalEngine()
    return engine.create_flow(version, channel, submitter, pre_check_id, emergency_reason)
