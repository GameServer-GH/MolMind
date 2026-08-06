"""SCP Hub MCP integration primitives.

The package deliberately keeps the transport independent from the Agent loop.  It
can be used with the official MCP SDK when the runtime is upgraded, or with the
small JSON-RPC compatibility client below while MolMind remains on Python 3.9.
"""

from .client import MCPClient, MCPError, MCPErrorCode
from .models import MCPContentBlock, MCPToolDescriptor, SCPObservation, SCPServerSpec, SCPSkillSpec
from .observations import observation_to_evidence_hit

__all__ = [
    "MCPClient", "MCPError", "MCPErrorCode", "MCPContentBlock",
    "MCPToolDescriptor", "SCPObservation", "SCPServerSpec", "SCPSkillSpec",
    "observation_to_evidence_hit",
]
