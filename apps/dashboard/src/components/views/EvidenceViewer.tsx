/**
 * 证据与源码溯源窗。
 *
 * **这是前后端分离最能说明问题的一块。** PDF 制品由后端生成并通过
 * `/api/v1/artifacts/spec.pdf` 提供，前端用 PDF.js 渲染；矩形标注框的
 * 坐标由 `/api/v1/traceability/anchors` 提供。后端换渲染方式、换文档来源，
 * 前端一行不用改 —— 只要那两个端点的契约不变。
 *
 * **坐标系的坑**：PDF 用户坐标原点在**左下角**，y 轴向上；画布坐标原点在
 * **左上角**，y 轴向下。直接拿 bbox 画矩形会上下颠倒。所以这里用
 * `convertToViewportPoint` 把 PDF 坐标的两个对角点换算到画布坐标，
 * 再自己求包围盒 —— 换算两次比换算一次更不容易错。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as pdfjsLib from 'pdfjs-dist';
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { FileCode2, FileText } from 'lucide-react';
import type { PdfAnchor, CodeAnchor } from '../../types/control-plane.ts';
import { fetchSpecPdf } from '../../api/client.ts';
import { describeError } from '../../hooks/useCockpitData.ts';
import { EmptyState, ErrorNote, LoadingState } from '../shared/Primitives.tsx';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;

interface PdfCanvasProps {
  anchor: PdfAnchor | null;
}

/** 把 PDF 用户坐标的矩形换算成画布坐标的矩形。 */
function pdfRectToCanvasRect(
  viewport: pdfjsLib.PageViewport,
  bbox: number[]
): { left: number; top: number; width: number; height: number } | null {
  if (bbox.length < 4) return null;
  const x = bbox[0];
  const y = bbox[1];
  const w = bbox[2];
  const h = bbox[3];
  if (
    typeof x !== 'number' ||
    typeof y !== 'number' ||
    typeof w !== 'number' ||
    typeof h !== 'number' ||
    [x, y, w, h].some((v) => Number.isNaN(v))
  ) {
    return null;
  }

  // 左下角与右上角（PDF 空间，y 向上）。
  const bottomLeft = viewport.convertToViewportPoint(x, y);
  const topRight = viewport.convertToViewportPoint(x + w, y + h);
  if (!bottomLeft || !topRight) return null;

  const left = Math.min(bottomLeft[0], topRight[0]);
  const top = Math.min(bottomLeft[1], topRight[1]);
  return {
    left,
    top,
    width: Math.abs(topRight[0] - bottomLeft[0]),
    height: Math.abs(topRight[1] - bottomLeft[1]),
  };
}

const PdfCanvas: React.FC<PdfCanvasProps> = ({ anchor }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const docRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null);
  // v6 里 `destroy()` 在 loading task 上，不在 document 上 —— 释放 worker 靠它。
  const taskRef = useRef<pdfjsLib.PDFDocumentLoadingTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 文档只加载一次，锚点变化时只重绘。
  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const blob = await fetchSpecPdf();
        const buffer = await blob.arrayBuffer();
        const loadingTask = pdfjsLib.getDocument({ data: buffer });
        taskRef.current = loadingTask;
        const doc = await loadingTask.promise;
        if (cancelled) {
          void loadingTask.destroy();
          return;
        }
        docRef.current = doc;
        setLoading(false);
      } catch (err) {
        if (!cancelled) {
          setError(describeError(err));
          setLoading(false);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
      docRef.current = null;
      const task = taskRef.current;
      taskRef.current = null;
      if (task) void task.destroy();
    };
  }, []);

  const draw = useCallback(async () => {
    const doc = docRef.current;
    const canvas = canvasRef.current;
    if (!doc || !canvas) return;

    const pageNumber = Math.min(Math.max(anchor?.page ?? 1, 1), doc.numPages);
    const page = await doc.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 1.4 });

    const context = canvas.getContext('2d');
    if (!context) return;

    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);

    await page.render({ canvas, canvasContext: context, viewport }).promise;

    // 叠加标注框。
    if (anchor?.bbox) {
      const rect = pdfRectToCanvasRect(viewport, anchor.bbox);
      if (rect) {
        context.save();
        context.strokeStyle = '#38bdf8';
        context.lineWidth = 2;
        context.fillStyle = 'rgba(56, 189, 248, 0.14)';
        context.fillRect(rect.left, rect.top, rect.width, rect.height);
        context.strokeRect(rect.left, rect.top, rect.width, rect.height);
        context.restore();
      }
    }
  }, [anchor]);

  useEffect(() => {
    if (loading || error) return;
    void draw();
  }, [draw, loading, error]);

  if (loading) return <LoadingState label="正在装载规范 PDF…" />;
  if (error) {
    return (
      <div className="p-3">
        <ErrorNote message={error} />
      </div>
    );
  }

  return (
    <div className="p-3 space-y-2">
      <canvas ref={canvasRef} className="w-full rounded border border-slate-800 bg-white" />
      {anchor ? (
        <div className="text-[11px] text-slate-400 leading-relaxed">
          <div className="text-slate-500">
            第 {anchor.page} 页 · {anchor.section}
          </div>
          <p className="mt-1 font-mono text-slate-300 bg-slate-950 border border-slate-800 rounded p-2">
            {anchor.text}
          </p>
        </div>
      ) : (
        <p className="text-[11px] text-slate-600">当前候选没有 PDF 锚点。</p>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// 溯源窗
// ---------------------------------------------------------------------------

interface EvidenceViewerProps {
  pdfAnchor: PdfAnchor | null | undefined;
  codeAnchor: CodeAnchor | null | undefined;
  title?: string;
}

const EvidenceViewer: React.FC<EvidenceViewerProps> = ({ pdfAnchor, codeAnchor, title }) => (
  <div className="flex flex-col h-full min-h-0">
    <div className="px-3 py-2 bg-slate-800/50 border-b border-slate-800 flex items-center gap-1.5 text-xs font-semibold text-slate-300 shrink-0">
      <FileText className="w-3.5 h-3.5 text-cyan-400" />
      {title ?? '证据与源码溯源'}
    </div>

    <div className="flex-1 min-h-0 overflow-y-auto divide-y divide-slate-800">
      {/* PDF 规范证据 */}
      <div>
        <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-slate-500 bg-slate-900/50">
          规范 PDF（PDF.js 高保真渲染）
        </div>
        <PdfCanvas anchor={pdfAnchor ?? null} />
      </div>

      {/* 源码证据 */}
      <div>
        <div className="px-3 py-1.5 text-[10px] uppercase tracking-wider text-slate-500 bg-slate-900/50 flex items-center gap-1.5">
          <FileCode2 className="w-3 h-3" />
          源码锚点
        </div>
        {codeAnchor ? (
          <div className="p-3">
            <div className="text-[11px] text-cyan-300 font-mono break-all">
              {codeAnchor.file}:{codeAnchor.line}
            </div>
            <pre className="mt-1.5 p-2.5 rounded bg-slate-950 border border-slate-800 text-[11px] font-mono text-slate-300 overflow-x-auto leading-relaxed">
              {codeAnchor.snippet}
            </pre>
          </div>
        ) : (
          <EmptyState label="当前候选没有源码锚点" />
        )}
      </div>
    </div>
  </div>
);

export default EvidenceViewer;
