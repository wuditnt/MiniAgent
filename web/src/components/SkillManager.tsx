/**
 * 技能管理页（占满主区）：
 *  · 自定义技能在上、系统内置在下，卡片流式布局（宽屏一行 4 个）；
 *  · 卡片：64×64 图标 + 名称 / 描述 + 启用开关（自定义多一个 ⋯ 菜单）；
 *  · 点击卡片查看 SKILL.md（Markdown 渲染），自定义可原地编辑；
 *  · 所有写操作（启停 / 编辑 / 删除）后端只对本机开放。
 */
import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  MoreOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Spin,
  Switch,
  Tag,
  Tooltip,
  message,
} from "antd";
import type { SkillDetail, SkillItem } from "../types";
import { deleteSkill, fetchSkillDetail, fetchSkills, saveSkillContent, setSkillEnabled } from "../api";
import Markdown from "./Markdown";
import SkillIcon from "./skillIcons";

interface Props {
  /** Markdown 里的相对资源按当前工作区解析 */
  workspaceId: string;
  onClose: () => void;
  /** 「在对话框中试用」：交给 App 切回对话并插入技能块 */
  onTrial: (skill: SkillItem) => void;
}

export default function SkillManager({ workspaceId, onClose, onTrial }: Props) {
  const [skills, setSkills] = useState<SkillItem[] | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    fetchSkills()
      .then(setSkills)
      .catch((e) => {
        setSkills([]);
        message.error(e instanceof Error ? e.message : "加载技能失败");
      });
  }, []);

  useEffect(load, [load]);

  const custom = (skills || []).filter((s) => s.source === "custom");
  const builtin = (skills || []).filter((s) => s.source === "builtin");

  const openDetail = (skill: SkillItem) => {
    setDetailLoading(true);
    setEditing(false);
    fetchSkillDetail(skill.id)
      .then(setDetail)
      .catch((e) => message.error(e instanceof Error ? e.message : "读取技能失败"))
      .finally(() => setDetailLoading(false));
  };

  const backToList = () => {
    setDetail(null);
    setEditing(false);
    setDraft("");
  };

  /** 启停：失败回滚（以接口返回的技能为准） */
  const toggle = async (skill: SkillItem, enabled: boolean) => {
    try {
      const updated = await setSkillEnabled(skill.id, enabled);
      setSkills((prev) =>
        (prev || []).map((s) => (s.id === updated.id ? updated : s))
      );
      setDetail((d) => (d && d.skill.id === updated.id ? { ...d, skill: updated } : d));
    } catch (e) {
      message.error(e instanceof Error ? e.message : "操作失败");
    }
  };

  const startEdit = () => {
    if (!detail) return;
    setDraft(detail.content);
    setEditing(true);
  };

  const cancelEdit = () => {
    if (!detail) return;
    if (draft === detail.content) {
      setEditing(false);
      return;
    }
    Modal.confirm({
      title: "放弃本次修改？",
      content: "未保存的内容会丢失。",
      okText: "放弃",
      cancelText: "继续编辑",
      okButtonProps: { danger: true },
      onOk: () => setEditing(false),
    });
  };

  const save = async () => {
    if (!detail) return;
    if (!draft.trim()) {
      message.warning("内容不能为空");
      return;
    }
    setSaving(true);
    try {
      const res = await saveSkillContent(detail.skill.id, draft);
      // 重新拉一次：正文要按新的 frontmatter 重新剥离，才能正确渲染
      const fresh = await fetchSkillDetail(res.skill.id);
      setDetail(fresh);
      setSkills((prev) =>
        (prev || []).map((s) => (s.id === res.skill.id ? res.skill : s))
      );
      setEditing(false);
      message.success("已保存");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  /** 软删除：目录移到 skills_user/.trash/，之后可以人工恢复 */
  const confirmDelete = (skill: SkillItem) => {
    Modal.confirm({
      title: `删除技能「${skill.name}」？`,
      content: "目录会被移到 skills_user/.trash/ 下（可恢复），但不会再出现在技能列表中。",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const res = await deleteSkill(skill.id);
          message.success(`已移入回收站：${res.trash}`);
          if (detail?.skill.id === skill.id) backToList();
          load();
        } catch (e) {
          message.error(e instanceof Error ? e.message : "删除失败");
          throw e; // 让确认框保持打开
        }
      },
    });
  };

  const card = (skill: SkillItem) => (
    <div
      className={`skill-card ${skill.enabled ? "" : "off"}`}
      key={skill.id}
      role="button"
      tabIndex={0}
      onClick={() => openDetail(skill)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openDetail(skill);
        }
      }}
    >
      <SkillIcon skill={skill} />
      <div className="skill-card-main">
        <div className="skill-card-name" title={skill.name}>
          {skill.name}
        </div>
        <div className="skill-card-desc" title={skill.description}>
          {skill.description}
        </div>
      </div>
      <div className="skill-card-ctrl" onClick={(e) => e.stopPropagation()}>
        {skill.source === "custom" && (
          <Dropdown
            trigger={["click"]}
            placement="bottomRight"
            menu={{
              items: [
                { key: "trial", icon: <ThunderboltOutlined />, label: "在对话框中试用" },
                { type: "divider" as const },
                { key: "delete", icon: <DeleteOutlined />, label: "删除", danger: true },
              ],
              onClick: ({ key }) => {
                if (key === "trial") onTrial(skill);
                else if (key === "delete") confirmDelete(skill);
              },
            }}
          >
            <span className="skill-card-more">
              <MoreOutlined />
            </span>
          </Dropdown>
        )}
        <Tooltip title={skill.enabled ? "已启用" : "已停用"}>
          <Switch
            size="small"
            checked={skill.enabled}
            onChange={(v) => toggle(skill, v)}
          />
        </Tooltip>
      </div>
    </div>
  );

  return (
    <div className="skill-page">
      <div className="skill-page-head">
        <div className="skill-page-head-left">
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={onClose}>
            返回对话
          </Button>
          <span className="skill-page-title">技能</span>
          {skills && <span className="skill-page-count">共 {skills.length} 个</span>}
        </div>
        <span className="skill-page-note">仅本机可管理</span>
      </div>

      <div className="skill-page-body">
        {detailLoading && (
          <div className="skill-page-loading">
            <Spin />
          </div>
        )}

        {!detailLoading && !detail && (
          <>
            <section className="skill-section">
              <div className="skill-section-head">
                <span>自定义技能</span>
                <span className="skill-section-count">{custom.length}</span>
              </div>
              {custom.length === 0 ? (
                <div className="skill-empty">
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={
                      <span>
                        还没有自定义技能。在 <code>skills_user/</code> 下新建一个含
                        <code> SKILL.md </code>的目录即可
                      </span>
                    }
                  />
                </div>
              ) : (
                <div className="skill-grid">{custom.map(card)}</div>
              )}
            </section>

            <section className="skill-section">
              <div className="skill-section-head">
                <span>系统内置技能</span>
                <span className="skill-section-count">{builtin.length}</span>
              </div>
              {builtin.length === 0 ? (
                <div className="skill-empty">没有内置技能</div>
              ) : (
                <div className="skill-grid">{builtin.map(card)}</div>
              )}
            </section>
          </>
        )}

        {!detailLoading && detail && (
          <div className="skill-detail">
            <div className="skill-detail-head">
              <Button
                type="text"
                icon={<ArrowLeftOutlined />}
                onClick={backToList}
                disabled={editing}
              >
                技能列表
              </Button>
              <div className="skill-detail-ident">
                <SkillIcon skill={detail.skill} size={44} />
                <div className="skill-detail-texts">
                  <div className="skill-detail-name">
                    {detail.skill.name}
                    <Tag color={detail.skill.source === "custom" ? "blue" : "default"}>
                      {detail.skill.source === "custom" ? "自定义" : "系统内置"}
                    </Tag>
                  </div>
                  <div className="skill-detail-desc">{detail.skill.description}</div>
                </div>
              </div>
              <div className="skill-detail-actions">
                {detail.skill.editable && !editing && (
                  <Button onClick={startEdit}>编辑</Button>
                )}
                <Tooltip title={detail.skill.enabled ? "已启用" : "已停用"}>
                  <Switch
                    checked={detail.skill.enabled}
                    onChange={(v) => toggle(detail.skill, v)}
                  />
                </Tooltip>
              </div>
            </div>

            <div className="skill-detail-path">{detail.path}/SKILL.md</div>

            {editing ? (
              <div className="skill-editor">
                <Input.TextArea
                  className="skill-editor-area"
                  value={draft}
                  spellCheck={false}
                  autoSize={{ minRows: 14, maxRows: 28 }}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <div className="skill-editor-foot">
                  <span className="skill-editor-hint">
                    {draft.length.toLocaleString()} 字符 · UTF-8 · 保存后立即生效
                  </span>
                  <Button onClick={cancelEdit} disabled={saving}>
                    取消
                  </Button>
                  <Button type="primary" loading={saving} onClick={save}>
                    保存
                  </Button>
                </div>
              </div>
            ) : (
              <div className="skill-detail-doc">
                <Markdown text={detail.body} workspaceId={workspaceId} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
