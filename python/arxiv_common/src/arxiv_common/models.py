"""Pydantic mirrors of schemas/messages.v1.json. The JSON Schema stays the contract; these are
typed conveniences, and every message is still validated against the schema on the way in/out."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Paper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    arxiv_id: str
    version: int = Field(ge=1)
    title: str
    abstract: str
    authors: list[str]
    primary_category: str
    categories: list[str]
    published_at: datetime
    updated_at: datetime
    pdf_url: str
    html_url: str | None = None
    doi: str | None = None
    journal_ref: str | None = None
    comment: str | None = None
    polled_at: datetime

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0)
    section: str | None = None
    text: str
    token_count: int = Field(ge=1)


TextSource = Literal["html", "pdf", "abstract"]


class ChunkedPaper(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    paper: Paper
    source: TextSource
    chunks: list[Chunk]
    fetched_at: datetime

    @classmethod
    def from_abstract(cls, paper: Paper, fetched_at: datetime) -> "ChunkedPaper":
        """Phase-2 path: no full text yet, the abstract is the single chunk."""
        text = f"{paper.title}\n\n{paper.abstract}"
        return cls(
            paper=paper,
            source="abstract",
            chunks=[Chunk(idx=0, section="abstract", text=text, token_count=max(1, len(text.split())))],
            fetched_at=fetched_at,
        )


class Failed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    arxiv_id: str
    stage: Literal["fetcher", "worker"]
    error: str
    attempts: int = Field(ge=1)
    failed_at: datetime
    payload: dict
