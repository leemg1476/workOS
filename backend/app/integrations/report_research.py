"""Opt-in, bounded hosted web research; store provenance without claiming fact verification."""
from typing import Annotated
from pydantic import BaseModel, Field, StringConstraints, ConfigDict

Domain = Annotated[str, StringConstraints(min_length=3,max_length=253,pattern=r'^([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$')]


class ReportExecutionOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    web_search: bool = False
    allowed_domains: list[Domain] = Field(default_factory=list, max_length=20)
    max_output_tokens: int = Field(default=500, ge=500, le=5000)
    max_tool_calls: int = Field(default=2, ge=1, le=3)
    timeout_seconds: int = Field(default=35, ge=35, le=180)


def request_options(options):
    result = {'max_output_tokens': options.max_output_tokens}
    if options.web_search:
        tool = {'type':'web_search','search_context_size':'medium'}
        if options.allowed_domains:
            tool['filters']={'allowed_domains':options.allowed_domains}
        result.update(tools=[tool], include=['web_search_call.action.sources'],
                      max_tool_calls=options.max_tool_calls)
    return result


def provenance(response):
    calls=[];citations=[]
    for item in getattr(response,'output',[]) or []:
        row=item.model_dump(mode='json') if hasattr(item,'model_dump') else item
        if row.get('type')=='web_search_call':
            calls.append(row)
        elif row.get('type')=='message':
            for part in row.get('content',[]):
                for annotation in part.get('annotations',[]):
                    if annotation.get('type')=='url_citation':
                        citations.append(annotation)
    return {'web_calls':calls,'citations':citations,
            'scope':'PROVIDER_URL_METADATA_NOT_INDEPENDENT_FACT_VERIFICATION'}
