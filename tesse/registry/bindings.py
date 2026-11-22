"""Resolve strategy declarations to built-in Python components, without executing them."""

from dataclasses import dataclass
from types import MappingProxyType

from ..forge.catalog import COMPONENTS, Component, ComponentContext
from ..forge.contracts import Invalid, keys, mapping, slug


@dataclass(frozen=True)
class ComponentBinding:
    component: Component
    params: MappingProxyType

    def invoke(self, *, context: ComponentContext = ComponentContext(), **inputs):
        """Compose offline using independently resolved settings and typed Python inputs."""
        params = {k: list(v) if isinstance(v, tuple) else v for k, v in self.params.items()}
        return self.component.invoke(context, params, **inputs)


def resolve_bindings(raw, field="component_bindings"):
    bindings = {}
    for name, binding in mapping(raw, field).items():
        label = f"{field}.{name}"
        slug(name, label)
        declaration = keys(binding, label, required=("component",), optional=("params",))
        key = declaration["component"]
        if not isinstance(key, str) or key not in COMPONENTS:
            raise Invalid(f"{label}.component", "versioned component not registered")
        component = COMPONENTS[key]
        params = component.resolve(declaration.get("params", {}), f"{label}.params")
        bindings[name] = ComponentBinding(
            component,
            MappingProxyType(
                {k: tuple(v) if isinstance(v, list) else v for k, v in params.items()}
            ),
        )
    return MappingProxyType(bindings)
