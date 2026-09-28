"""The one stack for the guardrails demo.

Owns everything the demo needs: the shared HTTP API, the Bedrock guardrail, the
API key, and each challenge construct mounted onto the API. C1 is the only
challenge today.

The guardrail and the key are created here rather than by hand beforehand, so a
clone is ``cdk deploy`` and nothing else. Both are torn down with the stack.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigwv2
from constructs import Construct

from c1.construct import C1Construct
from infra.api_key import ApiKey
from infra.guardrail import DemoGuardrail

#: Steady-state requests per second across the stage. This is a presenter-driven
#: demo, so a low ceiling is plenty and keeps a runaway loop cheap.
STAGE_RATE_LIMIT = 10
STAGE_BURST_LIMIT = 20


class SharedStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bot_model_id: str,
        bot_model_id_premium: str,
        judge_model_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.http_api = apigwv2.HttpApi(
            self,
            "Api",
            api_name=f"{construct_id}-api",
            description="Guardrails demo API.",
            cors_preflight=apigwv2.CorsPreflightOptions(
                # "*" already covers the Vite dev server on
                # http://localhost:5173 and any host the UI is served from.
                # Listing an origin alongside the wildcard would change nothing.
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                # Named explicitly, not "*". A wildcard never covers
                # Authorization, so a browser preflight would strip the API key
                # and every request would come back 401. The same applies to the
                # two custom headers the UI sends on POST /c1/attack: without
                # them the preflight fails and the browser never sends the
                # request at all.
                allow_headers=[
                    "authorization",
                    "content-type",
                    "x-model-tier",
                    "x-guardrail",
                ],
                max_age=Duration.days(1),
            ),
        )

        self._apply_stage_throttling()

        # Built from guardrail-config.json in the repo, so editing that file and
        # redeploying is the whole update path.
        self.guardrail = DemoGuardrail(self, "Guardrail")

        # Generated during the deploy and stored encrypted. Never in the template.
        # Named "ApiKeySecret" so the CfnOutput below can have the readable id.
        self.api_key = ApiKey(self, "ApiKeySecret")

        self.c1 = C1Construct(
            self,
            "C1",
            http_api=self.http_api,
            bot_model_id=bot_model_id,
            bot_model_id_premium=bot_model_id_premium,
            judge_model_id=judge_model_id,
            guardrail=self.guardrail,
            api_key=self.api_key,
        )

        self._declare_outputs()

    def _declare_outputs(self) -> None:
        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.api_endpoint,
            description="Base URL for the shared HTTP API.",
        )
        CfnOutput(
            self,
            "ApiKey",
            value=self.api_key.value,
            description=(
                "Shared API key for POST /c1/attack. Readable by anyone with "
                "DescribeStacks on this account; rotate or tear the stack down "
                "when the demo is over."
            ),
        )
        CfnOutput(
            self,
            "ApiKeyParameterArn",
            value=self.api_key.parameter_arn,
            description="SSM parameter the API key is stored in.",
        )
        CfnOutput(
            self,
            "GuardrailId",
            value=self.guardrail.guardrail_id,
            description="Bedrock guardrail built from guardrail-config.json.",
        )
        CfnOutput(
            self,
            "GuardrailVersion",
            value=self.guardrail.version,
            description="Version of that guardrail.",
        )
        CfnOutput(
            self,
            "C1AgentRuntimeArn",
            value=self.c1.agent_runtime_arn,
            description="ARN of the C1 QuickCart bot runtime.",
        )

    def _apply_stage_throttling(self) -> None:
        """Throttle the auto-created $default stage.

        HttpApi does not surface throttling on the default stage, so this reaches
        through to the underlying CfnStage.
        """
        default_stage = self.http_api.default_stage
        assert default_stage is not None, "HttpApi should create a default stage"
        cfn_stage = default_stage.node.default_child
        assert isinstance(cfn_stage, apigwv2.CfnStage)
        cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=STAGE_RATE_LIMIT,
            throttling_burst_limit=STAGE_BURST_LIMIT,
        )
