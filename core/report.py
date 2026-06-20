"""
演练与复盘报表模块
负责发布演练记录、熔断回滚复盘、并生成结构化HTML/JSON报表
"""
import os
import json
import uuid
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from jinja2 import Template

from .config import get_config

logger = logging.getLogger(__name__)


@dataclass
class DrillScenario:
    """演练场景"""
    scenario_id: str
    scenario_name: str
    scenario_type: str
    description: str
    expected_result: str
    actual_result: str = ""
    passed: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    issues: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DrillRecord:
    """完整演练记录"""
    drill_id: str
    drill_name: str
    drill_type: str
    description: str
    start_time: str
    end_time: str = ""
    participants: List[str] = field(default_factory=list)
    scenarios: List[DrillScenario] = field(default_factory=list)
    issues_found: List[str] = field(default_factory=list)
    action_items: List[Dict[str, Any]] = field(default_factory=list)
    lessons_learned: List[str] = field(default_factory=list)
    conclusion: str = ""
    overall_passed: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["scenarios"] = [s.to_dict() for s in self.scenarios]
        return data


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{{ title }}</title>
<style>
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei", Arial, sans-serif; margin: 0; padding: 30px; background: #f5f7fa; color: #333; }
.container { max-width: 1100px; margin: 0 auto; background: #fff; padding: 40px; border-radius: 10px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }
h1 { color: #1f2d3d; border-bottom: 3px solid #409eff; padding-bottom: 12px; margin-top: 0; }
h2 { color: #409eff; margin-top: 30px; }
h3 { color: #606266; }
.meta-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin: 20px 0; }
.meta-item { background: #f0f9ff; padding: 10px 14px; border-left: 4px solid #409eff; border-radius: 4px; }
.meta-item strong { color: #606266; display: block; font-size: 12px; margin-bottom: 4px; }
.meta-item span { color: #1f2d3d; font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin: 16px 0; }
th, td { border: 1px solid #ebeef5; padding: 10px 14px; text-align: left; font-size: 14px; }
th { background: #f5f7fa; color: #606266; }
tr:hover td { background: #fafbfc; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.badge-success { background: #f0f9eb; color: #67c23a; }
.badge-fail { background: #fef0f0; color: #f56c6c; }
.badge-warn { background: #fdf6ec; color: #e6a23c; }
.badge-info { background: #ecf5ff; color: #409eff; }
ul { line-height: 1.8; }
.section { margin: 24px 0; }
.footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #ebeef5; color: #909399; font-size: 12px; text-align: center; }
pre { background: #2d2d2d; color: #ccc; padding: 16px; border-radius: 6px; overflow: auto; font-size: 13px; }
</style>
</head>
<body>
<div class="container">
<h1>{{ title }}</h1>
<div class="meta-grid">
  {% for k, v in meta.items() %}
  <div class="meta-item"><strong>{{ k }}</strong><span>{{ v }}</span></div>
  {% endfor %}
</div>

{% if sections %}
{% for section in sections %}
<div class="section">
  <h2>{{ section.title }}</h2>
  {% if section.content %}<p>{{ section.content }}</p>{% endif %}
  {% if section.table %}
  <table>
    <thead><tr>{% for h in section.table.headers %}<th>{{ h }}</th>{% endfor %}</tr></thead>
    <tbody>
      {% for row in section.table.rows %}
      <tr>{% for cell in row %}<td>{{ cell }}</td>{% endfor %}</tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
  {% if section.list %}
  <ul>{% for item in section.list %}<li>{{ item }}</li>{% endfor %}</ul>
  {% endif %}
  {% if section.json %}<pre>{{ section.json }}</pre>{% endif %}
</div>
{% endfor %}
{% endif %}

<div class="footer">由自动分拣设备发布与回滚平台自动生成 · 生成时间 {{ generated_at }}</div>
</div>
</body>
</html>
"""


class ReportEngine:
    """报表引擎"""

    def __init__(self) -> None:
        self.config = get_config()
        self.output_dir = self.config.get("report.output_dir", "./data/reports")
        self.formats = list(self.config.get("report.formats", ["html", "json"]))
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)

    # ---------- 演练记录 ----------
    def create_drill(
        self,
        drill_name: str,
        drill_type: str = "circuit_breaker",
        description: str = "",
        participants: Optional[List[str]] = None,
    ) -> DrillRecord:
        """创建演练记录"""
        return DrillRecord(
            drill_id=f"DR{uuid.uuid4().hex[:12].upper()}",
            drill_name=drill_name,
            drill_type=drill_type,
            description=description,
            start_time=datetime.now().isoformat(),
            participants=list(participants or []),
        )

    def add_scenario(
        self,
        drill: DrillRecord,
        scenario_name: str,
        scenario_type: str,
        description: str,
        expected_result: str,
    ) -> DrillScenario:
        """向演练中添加场景"""
        scenario = DrillScenario(
            scenario_id=f"SC{uuid.uuid4().hex[:8].upper()}",
            scenario_name=scenario_name,
            scenario_type=scenario_type,
            description=description,
            expected_result=expected_result,
        )
        drill.scenarios.append(scenario)
        return scenario

    def complete_scenario(
        self,
        scenario: DrillScenario,
        actual_result: str,
        passed: bool,
        metrics: Optional[Dict[str, Any]] = None,
        issues: Optional[List[str]] = None,
        duration_seconds: float = 0.0,
    ) -> None:
        """完成演练场景"""
        scenario.actual_result = actual_result
        scenario.passed = passed
        scenario.metrics = dict(metrics or {})
        scenario.issues = list(issues or [])
        scenario.duration_seconds = duration_seconds

    def finalize_drill(
        self,
        drill: DrillRecord,
        issues_found: Optional[List[str]] = None,
        action_items: Optional[List[Dict[str, Any]]] = None,
        lessons_learned: Optional[List[str]] = None,
        conclusion: str = "",
    ) -> DrillRecord:
        """完成演练并保存"""
        drill.end_time = datetime.now().isoformat()
        drill.issues_found = list(issues_found or [])
        drill.action_items = list(action_items or [])
        drill.lessons_learned = list(lessons_learned or [])
        drill.conclusion = conclusion
        drill.overall_passed = all(s.passed for s in drill.scenarios) if drill.scenarios else False
        self.save_drill(drill)
        return drill

    def save_drill(self, drill: DrillRecord) -> List[str]:
        """保存演练记录为JSON和HTML"""
        paths: List[str] = []
        if "json" in self.formats:
            p = os.path.join(self.output_dir, f"drill_{drill.drill_id}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(drill.to_dict(), f, ensure_ascii=False, indent=2)
            paths.append(p)
        if "html" in self.formats:
            p = os.path.join(self.output_dir, f"drill_{drill.drill_id}.html")
            self._render_drill_html(drill, p)
            paths.append(p)
        logger.info("演练记录已保存: %s", paths)
        return paths

    def _render_drill_html(self, drill: DrillRecord, path: str) -> None:
        overall_badge = (
            '<span class="badge badge-success">通过</span>' if drill.overall_passed
            else '<span class="badge badge-fail">未通过</span>'
        )
        meta = {
            "演练ID": drill.drill_id,
            "演练名称": drill.drill_name,
            "演练类型": drill.drill_type,
            "开始时间": drill.start_time,
            "结束时间": drill.end_time or "进行中",
            "参与人员": ", ".join(drill.participants) or "无",
            "整体结果": overall_badge,
        }
        sections: List[Dict[str, Any]] = []
        sections.append({
            "title": "演练描述",
            "content": drill.description or "(无)",
        })

        if drill.scenarios:
            rows = []
            for s in drill.scenarios:
                badge = (
                    '<span class="badge badge-success">通过</span>' if s.passed
                    else '<span class="badge badge-fail">失败</span>'
                )
                rows.append([
                    s.scenario_name,
                    s.scenario_type,
                    s.description,
                    s.expected_result,
                    s.actual_result or "-",
                    f"{s.duration_seconds:.1f}s",
                    badge,
                ])
            sections.append({
                "title": "演练场景明细",
                "table": {
                    "headers": ["场景", "类型", "描述", "预期", "实际结果", "耗时", "状态"],
                    "rows": rows,
                },
            })
            for s in drill.scenarios:
                if s.issues:
                    sections.append({
                        "title": f"场景 [{s.scenario_name}] - 发现问题",
                        "list": s.issues,
                    })

        if drill.issues_found:
            sections.append({"title": "整体发现的问题", "list": drill.issues_found})
        if drill.action_items:
            rows = [
                [a.get("item", ""), a.get("owner", ""), a.get("deadline", ""), a.get("status", "")]
                for a in drill.action_items
            ]
            sections.append({
                "title": "Action Items",
                "table": {
                    "headers": ["事项", "责任人", "截止日期", "状态"],
                    "rows": rows,
                },
            })
        if drill.lessons_learned:
            sections.append({"title": "经验教训", "list": drill.lessons_learned})
        sections.append({"title": "结论", "content": drill.conclusion or "(无)"})

        html = Template(HTML_TEMPLATE).render(
            title=f"发布演练复盘报告 - {drill.drill_name}",
            meta=meta,
            sections=sections,
            generated_at=datetime.now().isoformat(),
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    # ---------- 通用发布/熔断报表 ----------
    def render_release_report(self, release_data: Dict[str, Any]) -> List[str]:
        """将灰度发布结果渲染为报表"""
        paths: List[str] = []
        rid = release_data.get("release_id", "unknown")
        cb = release_data.get("circuit_breaker_report")

        if "json" in self.formats:
            p = os.path.join(self.output_dir, f"release_{rid}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(release_data, f, ensure_ascii=False, indent=2)
            paths.append(p)

        if "html" in self.formats:
            p = os.path.join(self.output_dir, f"release_{rid}.html")
            status = release_data.get("status", "unknown")
            status_badge_map = {
                "completed": ("badge-success", "发布成功"),
                "triggered_circuit_breaker": ("badge-fail", "触发熔断"),
                "rolled_back": ("badge-warn", "已回滚"),
                "paused": ("badge-info", "已暂停"),
            }
            badge_cls, badge_text = status_badge_map.get(status, ("badge-info", status))
            status_badge = f'<span class="badge {badge_cls}">{badge_text}</span>'

            meta = {
                "发布ID": rid,
                "目标版本": release_data.get("version", ""),
                "基线版本": release_data.get("baseline_version", ""),
                "审批流ID": release_data.get("approval_flow_id", ""),
                "开始时间": release_data.get("start_time", ""),
                "结束时间": release_data.get("end_time", ""),
                "总耗时": f"{release_data.get('total_duration_seconds', 0):.2f}s",
                "最终状态": status_badge,
            }

            sections: List[Dict[str, Any]] = []
            stages = release_data.get("stages", [])
            if stages:
                rows = []
                for s in stages:
                    st = s.get("status", "unknown")
                    st_map = {
                        "stable": ("badge-success", "稳定"),
                        "deploying": ("badge-info", "部署中"),
                        "monitoring": ("badge-info", "监控中"),
                        "failed": ("badge-fail", "失败"),
                        "rolled_back": ("badge-warn", "已回滚"),
                        "pending": ("badge-info", "待执行"),
                    }
                    c, t = st_map.get(st, ("badge-info", st))
                    rows.append([
                        s.get("stage_id"),
                        s.get("stage_name"),
                        ", ".join(s.get("line_ids", [])),
                        f"{s.get('traffic_percentage', 0)}%",
                        f"{s.get('stable_monitor_minutes', 0)}min",
                        f'<span class="badge {c}">{t}</span>',
                    ])
                sections.append({
                    "title": "灰度阶段执行情况",
                    "table": {
                        "headers": ["序号", "阶段名", "分拣线", "流量占比", "稳定观察", "状态"],
                        "rows": rows,
                    },
                })

            if cb:
                breach_rows = [
                    [
                        b.get("metric_label"),
                        b.get("actual_value"),
                        b.get("threshold_value"),
                        ", ".join(b.get("line_ids", [])),
                        b.get("timestamp"),
                    ]
                    for b in cb.get("breaches", [])
                ]
                sections.append({
                    "title": f"熔断报告 - {cb.get('report_id', '')}",
                    "content": cb.get("summary", ""),
                })
                sections.append({
                    "title": "阈值突破明细",
                    "table": {
                        "headers": ["指标", "实际值", "阈值", "影响分拣线", "触发时间"],
                        "rows": breach_rows,
                    },
                })
                cb_meta_rows = [
                    ["触发阶段", cb.get("trigger_stage_name")],
                    ["影响分拣线", ", ".join(cb.get("affected_line_ids", []))],
                    ["触发时间", cb.get("trigger_time")],
                    ["回滚基线版本", cb.get("previous_stable_version")],
                    ["已自动回滚", "是" if cb.get("rollback_started") else "否"],
                    ["回滚完成时间", cb.get("rollback_completed_at") or "进行中"],
                    ["监控已重启", "是" if cb.get("monitor_restarted") else "否"],
                ]
                sections.append({
                    "title": "熔断处置详情",
                    "table": {"headers": ["项目", "值"], "rows": cb_meta_rows},
                })

            err = release_data.get("error_msg")
            if err:
                sections.append({"title": "错误信息", "content": err})

            html = Template(HTML_TEMPLATE).render(
                title=f"灰度发布报告 - {rid}",
                meta=meta,
                sections=sections,
                generated_at=datetime.now().isoformat(),
            )
            with open(p, "w", encoding="utf-8") as f:
                f.write(html)
            paths.append(p)

        logger.info("发布报表已生成: %s", paths)
        return paths

    def render_pre_check_report(self, check_data: Dict[str, Any]) -> List[str]:
        """将前置校验结果渲染为报表"""
        paths: List[str] = []
        cid = check_data.get("check_id", "unknown")

        if "json" in self.formats:
            p = os.path.join(self.output_dir, f"precheck_{cid}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(check_data, f, ensure_ascii=False, indent=2)
            paths.append(p)

        if "html" in self.formats:
            p = os.path.join(self.output_dir, f"precheck_{cid}.html")
            all_passed = check_data.get("all_passed", False)
            status_badge = (
                '<span class="badge badge-success">校验通过</span>' if all_passed
                else '<span class="badge badge-fail">校验阻断</span>'
            )
            channel_label = check_data.get("channel_label", check_data.get("channel", ""))
            meta = {
                "校验ID": cid,
                "版本号": check_data.get("version", ""),
                "发布通道": channel_label,
                "执行时间": check_data.get("executed_at", ""),
                "耗时": f"{check_data.get('duration_seconds', 0):.2f}s",
                "总体结果": status_badge,
                "摘要": check_data.get("summary", ""),
            }

            sections: List[Dict[str, Any]] = []

            rows = []
            status_map = {
                True: ("badge-success", "✓ 通过"),
                False: ("badge-fail", "✗ 阻断"),
            }
            for item in check_data.get("results", []):
                passed = item.get("passed", False)
                cls, txt = status_map.get(passed, status_map[False])
                d = item.get("details") or {}
                total = d.get("sample_size", d.get("total_samples", d.get("total_hours", "-")))
                passed_cnt = d.get(
                    "correct", d.get("running_hours", d.get("successful_scans", "-"))
                )
                failed_cnt = d.get(
                    "misclassified", d.get("downtime_hours", d.get("failed_scans", "-"))
                )
                if failed_cnt != "-" and isinstance(failed_cnt, int) and failed_cnt < 0:
                    failed_cnt = 0
                block_reason = "-"
                if not passed:
                    parts = []
                    if d.get("metric"):
                        parts.append(f"指标不达标")
                    if item.get("suggestion"):
                        parts.append(item["suggestion"])
                    block_reason = "; ".join(parts) if parts else "未达到阈值"
                rows.append([
                    item.get("check_name"),
                    f'<span class="badge {cls}">{txt}</span>',
                    item.get("actual_value", "-"),
                    item.get("threshold_value", "-"),
                    total, passed_cnt, failed_cnt,
                    block_reason,
                ])
            sections.append({
                "title": "核心指标明细",
                "table": {
                    "headers": ["校验项", "状态", "实际值", "阈值",
                                "样本总数", "通过样本", "未通过样本", "阻断原因"],
                    "rows": rows,
                },
            })

            blocking = check_data.get("blocking_items", [])
            if blocking:
                rows = [[b] for b in blocking]
                sections.append({
                    "title": f"阻断发布的校验项（{len(blocking)}项）",
                    "table": {"headers": ["阻断项"], "rows": rows},
                })

            html = Template(HTML_TEMPLATE).render(
                title=f"前置校验报告 - {cid}",
                meta=meta,
                sections=sections,
                generated_at=datetime.now().isoformat(),
            )
            with open(p, "w", encoding="utf-8") as f:
                f.write(html)
            paths.append(p)

        logger.info("前置校验报表已生成: %s", paths)
        return paths

    def cleanup_old_records(self, days: Optional[int] = None) -> int:
        """清理过期演练/报表记录"""
        retention = days or int(self.config.get("report.drill_retention_days", 90))
        cutoff = datetime.now() - timedelta(days=retention)
        removed = 0
        for fname in os.listdir(self.output_dir):
            fpath = os.path.join(self.output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
                    removed += 1
            except Exception as e:
                logger.warning("清理文件失败 %s: %s", fpath, e)
        logger.info("已清理 %d 条超过 %d 天的旧报表", removed, retention)
        return removed


def get_report_engine() -> ReportEngine:
    return ReportEngine()
