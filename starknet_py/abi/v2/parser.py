from __future__ import annotations

import dataclasses
import json
from collections import OrderedDict, defaultdict
from typing import DefaultDict, Dict, List, Optional, Tuple, TypeVar, Union, cast

from marshmallow import EXCLUDE

from starknet_py.abi.v2.model import Abi
from starknet_py.abi.v2.schemas import ContractAbiEntrySchema
from starknet_py.abi.v2.shape import (
    CONSTRUCTOR_ENTRY,
    ENUM_ENTRY,
    EVENT_ENTRY,
    FUNCTION_ENTRY,
    IMPL_ENTRY,
    INTERFACE_ENTRY,
    L1_HANDLER_ENTRY,
    STRUCT_ENTRY,
    ConstructorDict,
    EventDict,
    EventEnumVariantDict,
    EventStructMemberDict,
    FunctionDict,
    ImplDict,
    InterfaceDict,
    TypedParameterDict,
)
from starknet_py.cairo.data_types import CairoType, EnumType, EventType, StructType
from starknet_py.cairo.v2.type_parser import TypeParser


class AbiParsingError(ValueError):
    """
    Error raised when something wrong goes during abi parsing.
    """


class AbiParser:
    """
    Utility class for parsing abi into a dataclass.
    """

    # Entries from ABI grouped by entry type
    _grouped: DefaultDict[str, List[Dict]]
    # lazy init property
    _type_parser: Optional[TypeParser] = None

    def __init__(self, abi_list: List[Dict]):
        """
        Abi parser constructor. Ensures that abi satisfies the abi schema.

        :param abi_list: Contract's ABI as a list of dictionaries.
        """
        abi = [
            ContractAbiEntrySchema().load(entry, unknown=EXCLUDE) for entry in abi_list
        ]
        grouped = defaultdict(list)
        for entry in abi:
            assert isinstance(entry, dict)
            grouped[entry["type"]].append(entry)

        self._grouped = grouped

    def parse(self) -> Abi:
        """
        Parse abi provided to constructor and return it as a dataclass. Ensures that there are no cycles in the abi.

        :raises: AbiParsingError: on any parsing error.
        :return: Abi dataclass.
        """
        structures, enums = self._parse_structures_and_enums()
        events_dict = cast(
            Dict[str, EventDict],
            AbiParser._group_by_entry_name(
                self._grouped[EVENT_ENTRY], "defined events"
            ),
        )

        events: Dict[str, EventType] = {}
        for name, event in events_dict.items():
            events[name] = self._parse_event(event)
            assert self._type_parser is not None
            self._type_parser.add_defined_type(events[name])

        functions_dict = cast(
            Dict[str, FunctionDict],
            AbiParser._group_by_entry_name(
                self._grouped[FUNCTION_ENTRY], "defined functions"
            ),
        )
        interfaces_dict = cast(
            Dict[str, InterfaceDict],
            AbiParser._group_by_entry_name(
                self._grouped[INTERFACE_ENTRY], "defined interfaces"
            ),
        )
        impls_dict = cast(
            Dict[str, ImplDict],
            AbiParser._group_by_entry_name(self._grouped[IMPL_ENTRY], "defined impls"),
        )
        constructors = self._grouped[CONSTRUCTOR_ENTRY]
        l1_handlers = self._grouped[L1_HANDLER_ENTRY]

        if len(constructors) > 1:
            raise AbiParsingError("Constructor in ABI must be defined at most once.")

        if len(l1_handlers) > 1:
            raise AbiParsingError("L1 handler in ABI must be defined at most once.")

        return Abi(
            defined_structures=structures,
            defined_enums=enums,
            constructor=(
                self._parse_constructor(cast(ConstructorDict, constructors[0]))
                if constructors
                else None
            ),
            l1_handler=(
                self._parse_function(cast(FunctionDict, l1_handlers[0]))
                if l1_handlers
                else None
            ),
            functions={
                name: self._parse_function(entry)
                for name, entry in functions_dict.items()
            },
            events=events,
            interfaces={
                name: self._parse_interface(entry)
                for name, entry in interfaces_dict.items()
            },
            implementations={
                name: self._parse_impl(entry) for name, entry in impls_dict.items()
            },
        )

    @property
    def type_parser(self) -> TypeParser:
        if self._type_parser:
            return self._type_parser

        raise RuntimeError("Tried to get type_parser before it was set.")

    def _parse_structures_and_enums(
        self,
    ) -> Tuple[Dict[str, StructType], Dict[str, EnumType]]:
        structs_dict = AbiParser._group_by_entry_name(
            self._grouped[STRUCT_ENTRY], "defined structures"
        )
        enums_dict = AbiParser._group_by_entry_name(
            self._grouped[ENUM_ENTRY], "defined enums"
        )

        # Contains sorted members of the struct
        struct_members: Dict[str, List[TypedParameterDict]] = {}
        structs: Dict[str, StructType] = {}

        # Contains sorted members of the enum
        enum_members: Dict[str, List[TypedParameterDict]] = {}
        enums: Dict[str, EnumType] = {}

        # Example problem (with a simplified json structure):
        # [{name: User, fields: {id: Uint256}}, {name: "Uint256", ...}]
        # User refers to Uint256 even though it is not known yet (will be parsed next).
        # This is why it is important to create the structure types first. This way other types can already refer to
        # them when parsing types, even thought their fields are not filled yet.
        # At the end we will mutate those structures to contain the right fields. An alternative would be to use
        # topological sorting with an additional "unresolved type", so this flow is much easier.
        for name, struct in structs_dict.items():
            structs[name] = StructType(name, OrderedDict())
            struct_members[name] = struct["members"]

        for name, enum in enums_dict.items():
            enums[name] = EnumType(name, OrderedDict())
            enum_members[name] = enum["variants"]

        # Now parse the types of members and save them.
        defined_structs_enums: Dict[str, Union[StructType, EnumType]] = dict(structs)
        defined_structs_enums.update(enums)

        self._type_parser = TypeParser(defined_structs_enums)  # pyright: ignore
        for name, struct in structs.items():
            members = self._parse_members(
                cast(List[TypedParameterDict], struct_members[name]),
                f"members of structure '{name}'",
            )
            struct.types.update(members)
        for name, enum in enums.items():
            members = self._parse_members(
                cast(List[TypedParameterDict], enum_members[name]),
                f"members of enum '{name}'",
            )
            enum.variants.update(members)

        # All types have their members assigned now

        self._check_for_cycles(defined_structs_enums)

        return structs, enums

    @staticmethod
    def _check_for_cycles(structs: Dict[str, Union[StructType, EnumType]]):
        # We want to avoid creating our own cycle checker as it would make it more complex. json module has a built-in
        # checker for cycles.
        try:
            _to_json(structs)
        except ValueError as err:
            raise AbiParsingError(err) from ValueError

    def _parse_function(self, function: FunctionDict) -> Abi.Function:
        return Abi.Function(
            name=function["name"],
            inputs=self._parse_members(function["inputs"], function["name"]),
            outputs=list(
                self.type_parser.parse_inline_type(param["type"])
                for param in function["outputs"]
            ),
        )

    def _parse_constructor(self, constructor: ConstructorDict) -> Abi.Constructor:
        return Abi.Constructor(
            name=constructor["name"],
            inputs=self._parse_members(constructor["inputs"], constructor["name"]),
        )

    def _parse_event(self, event: EventDict) -> EventType:
        members_ = event.get("members", event.get("variants"))
        assert isinstance(members_, list)
        return EventType(
            name=event["name"],
            types=self._parse_members(
                cast(List[TypedParameterDict], members_), event["name"]
            ),
        )

    TypedParam = TypeVar(
        "TypedParam", TypedParameterDict, EventStructMemberDict, EventEnumVariantDict
    )

    def _parse_members(
        self, params: List[TypedParam], entity_name: str
    ) -> OrderedDict[str, CairoType]:
        # Without cast, it complains that 'Type "TypedParameterDict" cannot be assigned to type "T@_group_by_name"'
        members = AbiParser._group_by_entry_name(cast(List[Dict], params), entity_name)
        return OrderedDict(
            (name, self.type_parser.parse_inline_type(param["type"]))
            for name, param in members.items()
        )

    def _parse_interface(self, interface: InterfaceDict) -> Abi.Interface:
        return Abi.Interface(
            name=interface["name"],
            items=OrderedDict(
                (entry["name"], self._parse_function(entry))
                for entry in interface["items"]
            ),
        )

    @staticmethod
    def _parse_impl(impl: ImplDict) -> Abi.Impl:
        return Abi.Impl(
            name=impl["name"],
            interface_name=impl["interface_name"],
        )

    @staticmethod
    def _group_by_entry_name(
        dicts: List[Dict], entity_name: str
    ) -> OrderedDict[str, Dict]:
        grouped = OrderedDict()
        for entry in dicts:
            name = entry["name"]
            if name in grouped:
                raise AbiParsingError(
                    f"Name '{name}' was used more than once in {entity_name}."
                )
            grouped[name] = entry
        return grouped


def _to_json(value):
    class DataclassSupportingEncoder(json.JSONEncoder):
        def default(self, o):
            # Dataclasses are not supported by json. Additionally, dataclasses.asdict() works recursively and doesn't
            # check for cycles, so we need to flatten dataclasses (by ONE LEVEL) ourselves.
            if dataclasses.is_dataclass(o):
                return tuple(getattr(o, field.name) for field in dataclasses.fields(o))
            return super().default(o)

    return json.dumps(value, cls=DataclassSupportingEncoder)
