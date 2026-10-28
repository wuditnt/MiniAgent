/**
 * Sender 的自定义输入区：contenteditable 实现，支持「技能文字块」。
 *
 * 为什么不用 textarea：技能块要求是**整体不可编辑、可整块退格删除**的内联元素，
 * 原生 textarea 只能放纯文本。contenteditable 里用 `contenteditable=false` 的
 * span 正好满足（浏览器天然把它当原子节点：不可编辑、方向键整体跨过、退格整体删）。
 *
 * 受控协议：父组件传进来的 value 仍是纯字符串，技能块用私有标记编码
 * （见 skillMarks.ts）。用户输入后把 DOM 序列化回字符串上报；只有「值来自
 * 外部」时才重建 DOM，避免每次按键都重置光标。
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { Input } from "antd";
import type { GetProps } from "antd";
import { skillToken } from "./skillMarks";

type TextareaProps = GetProps<typeof Input.TextArea>;

const TOKEN_RE = /\u0001skill:([^\u0001]*)\u0001/g;

/** 单个节点 → 字符串：技能块还原成标记，<br> 与块级元素还原成换行 */
function serializeNode(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const el = node as HTMLElement;
  if (el.dataset.skill !== undefined) return skillToken(el.dataset.skill);
  if (el.tagName === "BR") return "\n";
  const inner = Array.from(el.childNodes).map(serializeNode).join("");
  // contenteditable 里回车会生成 <div>/<p>，它们代表换行
  return el.tagName === "DIV" || el.tagName === "P" ? "\n" + inner : inner;
}

function serialize(el: HTMLElement): string {
  return Array.from(el.childNodes)
    .map(serializeNode)
    .join("")
    .replace(/^\n+/, "");
}

function appendText(el: HTMLElement, text: string): void {
  text.split("\n").forEach((part, i) => {
    if (i > 0) el.appendChild(document.createElement("br"));
    if (part) el.appendChild(document.createTextNode(part));
  });
}

/** 受控字符串 → DOM：技能标记变成不可编辑的蓝色块 */
function renderInto(el: HTMLElement, value: string): void {
  el.textContent = "";
  let last = 0;
  let m: RegExpExecArray | null;
  TOKEN_RE.lastIndex = 0;
  while ((m = TOKEN_RE.exec(value))) {
    if (m.index > last) appendText(el, value.slice(last, m.index));
    const chip = document.createElement("span");
    chip.className = "skill-chip";
    chip.contentEditable = "false";
    chip.dataset.skill = m[1];
    chip.textContent = m[1];
    el.appendChild(chip);
    last = m.index + m[0].length;
  }
  if (last < value.length) appendText(el, value.slice(last));
}

function placeCaretAtEnd(el: HTMLElement): void {
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  range.selectNodeContents(el);
  range.collapse(false);
  sel.removeAllRanges();
  sel.addRange(range);
}

const SkillInput = forwardRef<{ focus: () => void; blur: () => void }, TextareaProps>(
  function SkillInput(props, ref) {
    const elRef = useRef<HTMLDivElement>(null);
    const composingRef = useRef(false);
    const value = typeof props.value === "string" ? props.value : "";

    useImperativeHandle(ref, () => ({
      focus: () => elRef.current?.focus(),
      blur: () => elRef.current?.blur(),
    }));

    // 每次渲染后都对齐一次：外部可能改写输入值（清空、插入技能、编辑重发回填），
    // 而依赖 [value] 的 effect 在「新旧值相同」时不会触发。用户正常输入时 DOM 与
    // value 相等，不会重建，光标不受影响；输入法组合期间跳过，避免打断。
    useEffect(() => {
      if (composingRef.current) return;
      const el = elRef.current;
      if (!el || serialize(el) === value) return;
      renderInto(el, value);
      placeCaretAtEnd(el);
    });

    const emit = () => {
      const el = elRef.current;
      if (!el) return;
      const next = serialize(el);
      props.onChange?.({
        target: { value: next },
      } as unknown as React.ChangeEvent<HTMLTextAreaElement>);
    };

    return (
      <div className="skill-input-wrap">
        <div
          ref={elRef}
          className={`skill-input-el ${props.className || ""}`}
          style={props.style}
          contentEditable={!props.disabled && !props.readOnly}
          role="textbox"
          aria-multiline="true"
          aria-label={typeof props["aria-label"] === "string" ? props["aria-label"] : undefined}
          suppressContentEditableWarning
          onInput={() => {
            if (!composingRef.current) emit();
          }}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
            emit();
          }}
          onKeyDown={(e) => {
            props.onKeyDown?.(e as unknown as React.KeyboardEvent<HTMLTextAreaElement>);
            if (e.defaultPrevented || composingRef.current) return;
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              props.onPressEnter?.(e as unknown as React.KeyboardEvent<HTMLTextAreaElement>);
            }
          }}
          onPaste={(e) => {
            // 带文件的粘贴交给 Sender 自己（走附件上传）
            if (e.clipboardData?.files?.length) {
              props.onPaste?.(e as unknown as React.ClipboardEvent<HTMLTextAreaElement>);
              return;
            }
            e.preventDefault();
            const text = e.clipboardData?.getData("text/plain") ?? "";
            document.execCommand("insertText", false, text);
          }}
        />
        {!value && props.placeholder && (
          <span className="skill-input-ph">{props.placeholder}</span>
        )}
      </div>
    );
  }
);

export default SkillInput;
