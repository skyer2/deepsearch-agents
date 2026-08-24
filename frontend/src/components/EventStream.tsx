import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  FileSearchOutlined,
  FlagOutlined,
  NodeIndexOutlined,
  OrderedListOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ScissorOutlined,
  ThunderboltOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Empty, Tag } from "antd";
import type { MonitorMessage } from "../types";

const PHASE_LABELS: Record<string, string> = {
  understand: "理解任务",
  plan: "生成计划",
  build_context: "构建上下文",
  execute: "执行 Agent",
  compress: "压缩上下文",
  validate: "结果校验",
  recover: "失败恢复",
  finalize: "完成交付",
  abort: "中止"
};

const PHASE_ORDER = [
  "understand",
  "plan",
  "build_context",
  "execute",
  "compress",
  "validate",
  "recover",
  "finalize"
];

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--:--:--";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function EventIcon({ event, phase }: { event: string; phase?: string }) {
  if (event === "phase") {
    if (phase === "understand") return <NodeIndexOutlined aria-hidden />;
    if (phase === "plan") return <OrderedListOutlined aria-hidden />;
    if (phase === "execute") return <ThunderboltOutlined aria-hidden />;
    if (phase === "compress") return <ScissorOutlined aria-hidden />;
    if (phase === "validate") return <SafetyCertificateOutlined aria-hidden />;
    if (phase === "recover") return <ReloadOutlined aria-hidden />;
    if (phase === "finalize") return <FlagOutlined aria-hidden />;
    return <ClockCircleOutlined aria-hidden />;
  }
  if (event === "assistant_call") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "tool_start") {
    return <ToolOutlined aria-hidden />;
  }
  if (event === "session_created") {
    return <FileSearchOutlined aria-hidden />;
  }
  if (event === "task_result") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "error") {
    return <CloseCircleOutlined aria-hidden />;
  }
  return <ClockCircleOutlined aria-hidden />;
}

function phaseStatusClass(status: string): string {
  if (status === "done") return "phase-step--done";
  if (status === "failed") return "phase-step--failed";
  if (status === "start") return "phase-step--running";
  return "phase-step--idle";
}

interface PhaseTimelineItem {
  phase: string;
  status: string;
  durationMs?: number;
  timestamp: string;
  data: Record<string, unknown>;
}

function buildPhaseTimeline(events: MonitorMessage[]): PhaseTimelineItem[] {
  const latestByPhase = new Map<string, PhaseTimelineItem>();

  for (const event of events) {
    if (event.event !== "phase") continue;
    const phase = String(event.data.phase ?? "");
    const status = String(event.data.status ?? "");
    if (!phase) continue;

    const durationRaw = event.data.duration_ms;
    const durationMs =
      typeof durationRaw === "number"
        ? durationRaw
        : typeof durationRaw === "string"
          ? Number(durationRaw)
          : undefined;

    latestByPhase.set(phase, {
      phase,
      status,
      durationMs: Number.isFinite(durationMs) ? durationMs : undefined,
      timestamp: event.timestamp,
      data: event.data
    });
  }

  return PHASE_ORDER.filter((phase) => latestByPhase.has(phase)).map(
    (phase) => latestByPhase.get(phase)!
  );
}

function HarnessPhaseTimeline({ items }: { items: PhaseTimelineItem[] }) {
  if (items.length === 0) {
    return null;
  }

  return (
    <div className="harness-phase-timeline" aria-label="Harness 阶段时间线">
      <div className="harness-phase-heading">
        <span className="panel-kicker">HARNESS LOOP</span>
        <strong>阶段时间线</strong>
      </div>
      <ol className="phase-step-list">
        {items.map((item) => (
          <li
            className={`phase-step ${phaseStatusClass(item.status)}`}
            key={`${item.phase}-${item.timestamp}`}
          >
            <div className="phase-step-icon">
              <EventIcon event="phase" phase={item.phase} />
            </div>
            <div className="phase-step-body">
              <div className="phase-step-title">
                <span>{PHASE_LABELS[item.phase] ?? item.phase}</span>
                <Tag color={item.status === "failed" ? "error" : item.status === "done" ? "success" : "processing"}>
                  {item.status}
                </Tag>
              </div>
              <div className="phase-step-meta">
                <time dateTime={item.timestamp}>{formatTime(item.timestamp)}</time>
                {item.durationMs !== undefined ? <span>{item.durationMs}ms</span> : null}
                {typeof item.data.step_index === "number" && typeof item.data.total_steps === "number" ? (
                  <span>
                    Step {item.data.step_index + 1}/{item.data.total_steps}
                  </span>
                ) : null}
                {typeof item.data.step_type === "string" ? <span>{item.data.step_type}</span> : null}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

interface EventStreamProps {
  events: MonitorMessage[];
}

export function EventStream({ events }: EventStreamProps) {
  const phaseTimeline = buildPhaseTimeline(events);

  return (
    <section className="console-panel event-panel" aria-labelledby="event-title">
      <div className="panel-heading">
        <div>
          <span className="panel-kicker">LIVE TRACE</span>
          <h2 id="event-title">实时执行轨迹</h2>
        </div>
        <span className="event-count">{events.length}</span>
      </div>

      <HarnessPhaseTimeline items={phaseTimeline} />

      {events.length === 0 ? (
        <div className="empty-console">
          <Empty
            description="等待 WebSocket 推送任务事件"
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        </div>
      ) : (
        <ol className="event-stream">
          {events.map((event, index) => {
            const phase = event.event === "phase" ? String(event.data.phase ?? "") : "";
            const status = event.event === "phase" ? String(event.data.status ?? "") : "";
            const rowClass = [
              "event-row",
              `event-row--${event.event}`,
              phase ? `event-row--phase-${phase}` : "",
              status === "failed" ? "event-row--phase-failed" : ""
            ]
              .filter(Boolean)
              .join(" ");

            return (
              <li className={rowClass} key={`${event.timestamp}-${index}`}>
                <div className="event-icon">
                  <EventIcon event={event.event} phase={phase} />
                </div>
                <div className="event-body">
                  <div className="event-meta">
                    <span>{event.event === "phase" ? `phase:${phase}` : event.event}</span>
                    <time dateTime={event.timestamp}>{formatTime(event.timestamp)}</time>
                  </div>
                  <p>{event.message}</p>
                  {Object.keys(event.data).length > 0 ? (
                    <pre>{JSON.stringify(event.data, null, 2)}</pre>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
