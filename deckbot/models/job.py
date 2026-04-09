from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CrawlChannelPayload(BaseModel):
  type: Literal["crawl_channel"] = "crawl_channel"
  channel_id: int


class ReprocessChannelPayload(BaseModel):
  type: Literal["reprocess_channel"] = "reprocess_channel"
  channel_id: int


# Union discriminated by "type" — extend here as new job types are added.
JobPayload = CrawlChannelPayload | ReprocessChannelPayload
