"use client";

import { useMemo, useState } from "react";
import { snapshotAt, worlds } from "./worlds";

type SortKey = "samples" | "score" | "heavy";

const formatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function scoreLabel(score: number | null) {
  if (score === null) return "没数据";
  if (score >= 90) return "目前挺稳";
  if (score >= 70) return "能玩";
  if (score >= 50) return "有点吃力";
  return "当前很重";
}

function scoreTone(score: number | null) {
  if (score === null) return "muted";
  if (score >= 90) return "good";
  if (score >= 70) return "okay";
  if (score >= 50) return "warn";
  return "bad";
}

export default function Home() {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortKey>("samples");

  const visibleWorlds = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    const filtered = worlds.filter((world) => {
      if (!needle) return true;
      return `${world.name} ${world.author} ${world.id}`
        .toLocaleLowerCase()
        .includes(needle);
    });

    return filtered.sort((a, b) => {
      if (sort === "samples") return b.samples - a.samples;
      if (sort === "heavy") {
        return (
          (a.score ?? Number.POSITIVE_INFINITY) -
          (b.score ?? Number.POSITIVE_INFINITY)
        );
      }
      return (b.score ?? -1) - (a.score ?? -1);
    });
  }, [query, sort]);

  return (
    <main>
      <header className="site-header">
        <a className="brand" href="#top" aria-label="FramePilot World Bench 首页">
          <span className="brand-mark">FP</span>
          <span>
            <strong>World Bench</strong>
            <small>by FramePilot VR</small>
          </span>
        </a>
        <span className="live-pill">
          <i />
          粗糙预览版
        </span>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="eyebrow">VRCHAT · PCVR · COMMUNITY TELEMETRY</p>
          <h1>
            地图负载，
            <br />
            <span>先把榜跑起来。</span>
          </h1>
          <p className="hero-note">
            真实游玩产生的 FramePilot 聚合数据。现在只有一台机器，分数就是
            “稳定窗口占比”——先看个方向，千万别当圣经。
          </p>
        </div>

        <div className="stat-panel">
          <div><strong>17</strong><span>收录地图</span></div>
          <div><strong>2,380</strong><span>遥测记录</span></div>
          <div><strong>1</strong><span>贡献设备</span></div>
          <p>数据快照 · {snapshotAt}</p>
        </div>
      </section>

      <section className="bench-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">THE VERY EARLY LIST</p>
            <h2>当前观测榜</h2>
          </div>
          <p>高分 = 在当时人数、分辨率和帧率设置下，更多窗口满足帧预算。</p>
        </div>

        <div className="toolbar">
          <label className="search">
            <span>⌕</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜地图、作者或 World ID"
              aria-label="搜索地图"
            />
          </label>
          <label className="sort">
            <span>排序</span>
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as SortKey)}
              aria-label="地图排序"
            >
              <option value="samples">样本最多</option>
              <option value="score">分数最高</option>
              <option value="heavy">负载最重</option>
            </select>
          </label>
        </div>

        <div className="result-line">
          找到 {visibleWorlds.length} 张地图 <span>·</span> 分数没有剥离玩家负载
        </div>

        <div className="world-grid">
          {visibleWorlds.map((world, index) => (
            <article className="world-card" key={world.id}>
              <div className="world-image">
                <img
                  src={world.thumbnail}
                  alt=""
                  loading="lazy"
                  referrerPolicy="no-referrer"
                />
                <span className="rank">#{index + 1}</span>
                <span className={`score score-${scoreTone(world.score)}`}>
                  <strong>{world.score ?? "—"}</strong>
                  <small>稳定分</small>
                </span>
              </div>

              <div className="world-body">
                <div className="world-title">
                  <div>
                    <h3>{world.name}</h3>
                    <p>by {world.author}</p>
                  </div>
                  <span className={`verdict verdict-${scoreTone(world.score)}`}>
                    {scoreLabel(world.score)}
                  </span>
                </div>

                {world.samples > 0 ? (
                  <dl className="metrics">
                    <div><dt>GPU P95</dt><dd>{world.gpuMs} ms</dd></div>
                    <div><dt>CPU P95</dt><dd>{world.cpuMs} ms</dd></div>
                    <div>
                      <dt>测试人数</dt>
                      <dd>{world.populationMin}–{world.populationMax}</dd>
                    </div>
                  </dl>
                ) : (
                  <div className="empty-metrics">
                    有 {world.transitionRecords} 条人数变化记录，但没有可评分的稳定窗口。
                  </div>
                )}

                <div className="tags">
                  <span>{world.mode}</span>
                  {world.scale !== null && <span>{world.scale}% 分辨率</span>}
                  <span>{world.samples} 窗口</span>
                </div>

                <div className="world-footer">
                  <span>{formatter.format(world.visits)} visits</span>
                  <span>♥ {formatter.format(world.favorites)}</span>
                  <a
                    href={`https://vrchat.com/home/world/${world.id}`}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`在 VRChat 查看 ${world.name}`}
                  >
                    查看地图 ↗
                  </a>
                </div>
              </div>
            </article>
          ))}
        </div>

        {visibleWorlds.length === 0 && (
          <div className="no-results">没有这张图，或者还没人带着 FramePilot 去过。</div>
        )}
      </section>

      <section className="method">
        <div>
          <p className="eyebrow">HOW BAD IS THE MATH?</p>
          <h2>现在的算法，三行。</h2>
        </div>
        <ol>
          <li>取地图里的 60 秒稳定窗口。</li>
          <li>检查 GPU、CPU、重投影和丢帧有没有越线。</li>
          <li>达标窗口比例 × 100，就是当前分数。</li>
        </ol>
        <p>
          它混着地图、玩家 Avatar、人数、硬件和分辨率。等贡献者多了，再做标准化和玩家负载剥离。
        </p>
      </section>

      <footer>
        <p>FramePilot World Bench · 独立社区项目，与 VRChat Inc. 无关联。</p>
        <p>匿名聚合 · 不包含玩家身份、实例 ID 或硬件序列号</p>
      </footer>
    </main>
  );
}
