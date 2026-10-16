/**
 * 工作区新建/编辑弹窗：名称 + 路径。路径必须是已存在的本地目录（后端二次校验）。
 * 新建流程：父组件先弹原生文件夹选择框，选中后路径回填、名称自动取文件夹名
 * （仍可手改）；弹窗内也保留「选择」按钮可重新选。
 */
import { useEffect, useState } from "react";
import { Button, Form, Input, Modal, Space } from "antd";
import { FolderOpenOutlined } from "@ant-design/icons";
import { pickFolder } from "../api";
import type { WorkspaceItem } from "../types";

/** 从路径取文件夹名：去掉尾部分隔符取最后一段；根目录（如 D:\）退回盘符 */
function folderNameOf(p: string): string {
  const trimmed = p.replace(/[\\/]+$/, "");
  const base = trimmed.split(/[\\/]/).pop() || "";
  return base || trimmed;
}

interface Props {
  open: boolean;
  mode: "create" | "edit";
  initial?: WorkspaceItem | null;
  /** create 模式下父组件预先选好的文件夹路径（新建流程会先弹原生选择框） */
  pickedPath?: string | null;
  onCancel: () => void;
  /** 提交：返回 Promise，失败时错误会显示在弹窗里 */
  onSubmit: (name: string, path: string) => Promise<void>;
}

export default function WorkspaceModal({
  open,
  mode,
  initial,
  pickedPath,
  onCancel,
  onSubmit,
}: Props) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [picking, setPicking] = useState(false);

  // 打开时回填：create 优先用父组件预选的文件夹；edit 用工作区原值
  useEffect(() => {
    if (!open) return;
    if (mode === "create" && pickedPath) {
      setPath(pickedPath);
      setName(folderNameOf(pickedPath));
    } else {
      setName(initial?.name || "");
      setPath(initial?.path || "");
    }
    setError("");
  }, [open, mode, initial, pickedPath]);

  /** 弹窗内（重新）选择文件夹；create 模式下名称跟着文件夹名走 */
  const pick = async () => {
    setPicking(true);
    try {
      const p = await pickFolder();
      if (p) {
        setPath(p);
        if (mode === "create") setName(folderNameOf(p));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPicking(false);
    }
  };

  const submit = async () => {
    if (!name.trim()) {
      setError("请填写工作区名称");
      return;
    }
    if (!path.trim()) {
      setError("请填写工作区路径");
      return;
    }
    setSaving(true);
    try {
      await onSubmit(name.trim(), path.trim());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={mode === "create" ? "新建工作区" : "编辑工作区"}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      onOk={submit}
      onCancel={onCancel}
      destroyOnClose
    >
      <Form layout="vertical" style={{ marginTop: 8 }}>
        <Form.Item label="名称" required style={{ marginBottom: 12 }}>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：我的项目"
            maxLength={40}
          />
        </Form.Item>
        <Form.Item
          label="路径"
          required
          style={{ marginBottom: 4 }}
          extra="必须是本机已存在的目录，将作为 Agent 的文件操作边界"
        >
          <Space.Compact style={{ width: "100%" }}>
            <Input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="如：D:\work\demo"
            />
            <Button
              icon={<FolderOpenOutlined />}
              loading={picking}
              onClick={pick}
              title="浏览本机文件夹"
            >
              选择
            </Button>
          </Space.Compact>
        </Form.Item>
        {error && <div className="ws-modal-error">{error}</div>}
      </Form>
    </Modal>
  );
}
