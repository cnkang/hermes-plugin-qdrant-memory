"""Generate a larger synthetic retrieval fixture with hard negatives; no personal data."""

import json
import pathlib
from collections import OrderedDict

PROJECTS = [
    ("Cedar", "cedar"),
    ("Cedar-II", "cedar-ii"),
    ("Cedra", "cedra"),
    ("Rowan", "rowan"),
    ("Rowan-Labs", "rowan-labs"),
    ("Maple", "maple"),
    ("Maple-Core", "maple-core"),
    ("Larch", "larch"),
    ("Birch", "birch"),
    ("Aspen", "aspen"),
    ("Willow", "willow"),
    ("Sequoia", "sequoia"),
    ("Juniper", "juniper"),
    ("Alder", "alder"),
    ("Hazel", "hazel"),
    ("Pine", "pine"),
    ("Fir", "fir"),
    ("Elm", "elm"),
    ("Ash", "ash"),
    ("Oak", "oak"),
    ("Sable", "sable"),
    ("Tamarack", "tamarack"),
    ("Cypress", "cypress"),
    ("Magnolia", "magnolia"),
]

DATABASES = ["SQLite WAL", "PostgreSQL", "MariaDB", "DuckDB", "CockroachDB", "MySQL"]
CACHES = ["Redis", "Memcached", "the local file cache", "Valkey", "Hazelcast"]
REGIONS = ["Frankfurt", "Singapore", "Oregon", "Dublin", "Sao Paulo", "Tokyo"]
OWNERS = [
    "the platform team",
    "the data team",
    "the release team",
    "the runtime team",
    "the security team",
]
LANGUAGES = ["Rust", "Python", "Go", "TypeScript", "Kotlin", "C++"]
CIS = ["Ubuntu 24.04", "Ubuntu 22.04", "Debian 12", "Windows Server 2025", "macOS 26"]
RETRIES = [2, 3, 5, 8]
ZONES = ["UTC", "Europe/Berlin", "Asia/Singapore", "America/Los_Angeles"]
LOCKS = ["a local file lease", "an advisory lock", "a database row lock", "an S3 conditional write"]
MODELS = ["qwen3-embedding:4b", "bge-m3", "nomic-embed-text", "mxbai-embed-large"]


def attributes(name, index):
    """Return one deterministic value set per project, with deliberate collisions."""
    return OrderedDict(
        [
            ("database", DATABASES[index % len(DATABASES)]),
            ("cache", CACHES[index % len(CACHES)]),
            ("region", REGIONS[index % len(REGIONS)]),
            ("owner", OWNERS[index % len(OWNERS)]),
            ("language", LANGUAGES[index % len(LANGUAGES)]),
            ("ci", CIS[index % len(CIS)]),
            ("retries", RETRIES[index % len(RETRIES)]),
            ("zone", ZONES[index % len(ZONES)]),
            ("lock", LOCKS[index % len(LOCKS)]),
            ("model", MODELS[index % len(MODELS)]),
            ("docs", "docs/architecture.md" if index % 2 else "docs/design/overview.md"),
            ("pin", f"0.{7 if index % 2 else 6}.{index % 4}"),
            ("backup", "nightly encrypted backups" if index % 3 else "weekly encrypted backups"),
            (
                "release",
                "run the suite, verify packaging, then tag"
                if index % 2
                else "verify the wheel hash, then tag",
            ),
            ("review", "English" if index % 2 else "Chinese"),
        ]
    )


TEXT = {
    "database": "Project {name} uses {database} for its primary database.",
    "cache": "Project {name} keeps session data in {cache}.",
    "region": "The staging region for Project {name} is {region}.",
    "owner": "The deployment owner for Project {name} is {owner}.",
    "language": "Project {name} is implemented mainly in {language}.",
    "ci": "Project {name} runs CI on {ci}.",
    "retries": "Project {name} retries failed jobs {retries} times.",
    "zone": "Project {name} writes production logs in the {zone} timezone.",
    "lock": "Project {name} serializes writers with {lock}.",
    "model": "Project {name} generates embeddings with {model}.",
    "docs": "Project {name} keeps architecture notes in {docs}.",
    "pin": "Project {name} pins the runtime at version {pin}.",
    "backup": "Project {name} keeps {backup}.",
    "release": "The release procedure for Project {name} is to {release}.",
    "review": "The project owner for {name} prefers release notes in {review}.",
}

QUERY = {
    "database": {
        "en": "Which database does Project {name} use for its primary store?",
        "zh": "Project {name} 的主数据库是什么？",
        "mixed": "Project {name} primary database 用的是哪个？",
    },
    "cache": {
        "en": "Where does Project {name} keep session data?",
        "zh": "Project {name} 把会话数据放在哪里？",
        "mixed": "Project {name} 的 session data 存在哪个 cache？",
    },
    "region": {
        "en": "Which region hosts Project {name} staging?",
        "zh": "Project {name} 的 staging 在哪个区域？",
        "mixed": "Project {name} staging region 是哪里？",
    },
    "owner": {
        "en": "Which team owns Project {name} deployments?",
        "zh": "Project {name} 的部署由哪个团队负责？",
        "mixed": "Project {name} 的 deployment owner 是哪个 team？",
    },
    "language": {
        "en": "Which language is Project {name} written in?",
        "zh": "Project {name} 主要用什么语言实现？",
        "mixed": "Project {name} 主要语言是哪个 language？",
    },
    "ci": {
        "en": "Which operating system runs Project {name} CI?",
        "zh": "Project {name} 的 CI 跑在哪个系统上？",
        "mixed": "Project {name} CI 跑在哪个 OS？",
    },
    "retries": {
        "en": "How many retries does Project {name} allow for failed jobs?",
        "zh": "Project {name} 的失败任务重试几次？",
        "mixed": "Project {name} 的 retry 次数是多少？",
    },
    "zone": {
        "en": "Which timezone does Project {name} use for production logs?",
        "zh": "Project {name} 的生产日志用哪个时区？",
        "mixed": "Project {name} 的 production logs 用哪个 timezone？",
    },
    "lock": {
        "en": "How does Project {name} serialize writers?",
        "zh": "Project {name} 用什么机制串行化写入者？",
        "mixed": "Project {name} 的 writer 串行化用什么 lock？",
    },
    "model": {
        "en": "Which embedding model does Project {name} use?",
        "zh": "Project {name} 用哪个 embedding 模型？",
        "mixed": "Project {name} 用哪个 embedding model？",
    },
    "docs": {
        "en": "Where does Project {name} keep architecture notes?",
        "zh": "Project {name} 的架构文档放在哪里？",
        "mixed": "Project {name} architecture notes 的路径是什么？",
    },
    "pin": {
        "en": "Which runtime version does Project {name} pin?",
        "zh": "Project {name} 固定使用哪个 runtime 版本？",
        "mixed": "Project {name} 的 runtime pin 是哪个版本？",
    },
    "review": {
        "en": "Which release-note language does the owner of {name} prefer?",
        "zh": "{name} 的项目负责人偏好哪种发布说明语言？",
        "mixed": "{name} 的 owner 偏好哪个 release-note language？",
    },
}


def main():
    """Write the fixture deterministically so repeated runs produce identical bytes."""
    memories, index = [], {}
    for position, (name, slug) in enumerate(PROJECTS):
        values = attributes(name, position)
        for attribute, template in TEXT.items():
            identifier = f"{slug}-{attribute}"
            memories.append({"id": identifier, "text": template.format(name=name, **values)})
            index[identifier] = (name, attribute, values[attribute])

    queries = []
    labelled = [
        (0, "database", "en"),
        (3, "database", "zh"),
        (6, "database", "mixed"),
        (1, "cache", "en"),
        (5, "cache", "zh"),
        (9, "cache", "mixed"),
        (2, "region", "en"),
        (7, "region", "zh"),
        (11, "region", "mixed"),
        (4, "owner", "en"),
        (8, "owner", "zh"),
        (13, "owner", "mixed"),
        (10, "language", "en"),
        (14, "language", "zh"),
        (17, "language", "mixed"),
        (12, "ci", "en"),
        (16, "ci", "zh"),
        (19, "ci", "mixed"),
        (15, "retries", "en"),
        (18, "retries", "zh"),
        (20, "retries", "mixed"),
        (21, "zone", "en"),
        (22, "zone", "zh"),
        (23, "zone", "mixed"),
        (0, "lock", "en"),
        (4, "lock", "zh"),
        (8, "lock", "mixed"),
        (2, "model", "en"),
        (6, "model", "zh"),
        (10, "model", "mixed"),
        (1, "docs", "en"),
        (5, "docs", "zh"),
        (9, "docs", "mixed"),
        (3, "pin", "en"),
        (7, "pin", "zh"),
        (11, "pin", "mixed"),
        (12, "review", "en"),
        (15, "review", "zh"),
        (18, "review", "mixed"),
        (21, "review", "en"),
        (22, "review", "zh"),
        (23, "review", "mixed"),
        (13, "database", "zh"),
        (18, "database", "mixed"),
        (14, "cache", "mixed"),
        (21, "cache", "en"),
        (16, "region", "en"),
        (20, "region", "zh"),
        (17, "owner", "zh"),
        (22, "owner", "mixed"),
        (19, "language", "en"),
        (23, "language", "zh"),
        (13, "ci", "zh"),
        (22, "ci", "mixed"),
        (14, "retries", "en"),
        (23, "retries", "zh"),
        (15, "zone", "mixed"),
        (19, "zone", "en"),
        (16, "lock", "zh"),
        (20, "lock", "mixed"),
        (17, "model", "en"),
        (21, "model", "zh"),
        (18, "docs", "mixed"),
        (22, "docs", "en"),
        (19, "pin", "zh"),
        (23, "pin", "mixed"),
    ]
    for position, attribute, language in labelled:
        name, slug = PROJECTS[position]
        queries.append(
            {
                "query": QUERY[attribute][language].format(name=name),
                "relevant_ids": [f"{slug}-{attribute}"],
                "category": language,
            }
        )

    # Multi-relevant: one question whose answer spans two projects that share a value.
    queries.append(
        {
            "query": "Which projects use a local file lease to serialize writers?",
            "relevant_ids": ["cedar-lock", "rowan-labs-lock", "sequoia-lock", "sable-lock"],
            "category": "multi-relevant",
        }
    )
    queries.append(
        {
            "query": "哪些项目使用 SQLite WAL 作为主数据库？",
            "relevant_ids": [
                "cedar-database",
                "maple-core-database",
                "elm-database",
                "magnolia-database",
            ],
            "category": "multi-relevant",
        }
    )

    # Abstention: no memory answers these. Scored separately, never as a recall miss.
    for text in (
        "Which database does Project Larch use for its billing service?",
        "Project Tamarack 的创始人是谁？",
        "What is the holiday schedule for the platform team?",
        "Project Magnolia 的 marketing budget 是多少？",
    ):
        queries.append({"query": text, "relevant_ids": [], "category": "abstention"})

    fixture = {
        "version": "synthetic-retrieval-scale-2026-11-26-v1",
        "description": (
            "24 synthetic projects with shared attribute values and near-identical names; "
            "72 labelled queries across English, Chinese, mixed and multi-relevant slices, "
            "plus 4 abstention queries with no relevant memory."
        ),
        "memories": memories,
        "queries": queries,
        "allow_abstention": True,
    }
    path = pathlib.Path("tests/fixtures/retrieval-scale-v1.json")
    path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(len(memories), "memories", len(queries), "queries", path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
