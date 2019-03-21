import json
import re

from cytoolz import (
    curry,
    pipe,
)
from eth_abi import (
    encode_abi,
    is_encodable,
)
from eth_utils import (
    keccak,
    to_bytes,
    to_int,
    to_text,
)

from eth_account._utils.transactions import (
    ChainAwareUnsignedTransaction,
    UnsignedTransaction,
    encode_transaction,
    serializable_unsigned_transaction_from_dict,
    strip_signature,
)

CHAIN_ID_OFFSET = 35
V_OFFSET = 27

# signature versions
PERSONAL_SIGN_VERSION = b'E'  # Hex value 0x45
INTENDED_VALIDATOR_SIGN_VERSION = b'\x00'  # Hex value 0x00
STRUCTURED_DATA_SIGN_VERSION = b'\x01'  # Hex value 0x01

# Regexes
IDENTIFIER_REGEX = r"^[a-zA-Z_$][a-zA-Z_$0-9]*$"
TYPE_REGEX = r"^[a-zA-Z_$][a-zA-Z_$0-9]*(\[([1-9]\d*)*\])*$"


def sign_transaction_dict(eth_key, transaction_dict):
    # generate RLP-serializable transaction, with defaults filled
    unsigned_transaction = serializable_unsigned_transaction_from_dict(transaction_dict)

    transaction_hash = unsigned_transaction.hash()

    # detect chain
    if isinstance(unsigned_transaction, UnsignedTransaction):
        chain_id = None
    else:
        chain_id = unsigned_transaction.v

    # sign with private key
    (v, r, s) = sign_transaction_hash(eth_key, transaction_hash, chain_id)

    # serialize transaction with rlp
    encoded_transaction = encode_transaction(unsigned_transaction, vrs=(v, r, s))

    return (v, r, s, encoded_transaction)


#
# EIP712 Functionalities
#
def dependencies(primaryType, types, found=None):
    """
    Recursively get all the dependencies of the primaryType
    """
    # This is done to avoid the by-reference call of python
    found = found or []

    if primaryType in found:
        return found
    if primaryType not in types:
        return found

    found.append(primaryType)
    for field in types[primaryType]:
        for dep in dependencies(field["type"], types, found):
            if dep not in found:
                found.push(dep)

    return found


def dict_to_type_name_converter(field):
    """
    Given a dictionary ``field`` of type {'name': NAME, 'type': TYPE},
    this function converts it to ``TYPE NAME``
    """
    return field["type"] + " " + field["name"]


def encodeType(primaryType, types):
    """
    The type of a struct is encoded as name ‖ "(" ‖ member₁ ‖ "," ‖ member₂ ‖ "," ‖ … ‖ memberₙ ")"
    where each member is written as type ‖ " " ‖ name.
    """
    # Getting the dependencies and sorting them alphabetically as per EIP712
    deps = dependencies(primaryType, types)
    deps_without_primary_type = list(filter(lambda x: x != primaryType, deps))
    sorted_deps = [primaryType] + sorted(deps_without_primary_type)

    result = ''.join(
        [
            struct_name + "(" + ','.join(map(dict_to_type_name_converter, types[struct_name])) + ")"
            for struct_name in sorted_deps
        ]
    )
    return result


def typeHash(primaryType, types):
    return keccak(text=encodeType(primaryType, types))


def encodeData(primaryType, types, data):
    encTypes = []
    encValues = []

    # Add typehash
    encTypes.append("bytes32")
    encValues.append(typeHash(primaryType, types))

    # Add field contents
    for field in types[primaryType]:
        value = data[field["name"]]
        if field["type"] == "string":
            if not isinstance(value, str):
                raise TypeError(
                    "Value of `{0}` ({2}) in the struct `{1}` is of the type `{3}`, but expected "
                    "string value".format(
                        field["name"],
                        primaryType,
                        value,
                        type(value),
                    )
                )
            # Special case where the values need to be keccak hashed before they are encoded
            encTypes.append("bytes32")
            hashed_value = keccak(text=value)
            encValues.append(hashed_value)
        elif field["type"] == "bytes":
            if not isinstance(value, bytes):
                raise TypeError(
                    "Value of `{0}` ({2}) in the struct `{1}` is of the type `{3}`, but expected "
                    "bytes value".format(
                        field["name"],
                        primaryType,
                        value,
                        type(value),
                    )
                )
            # Special case where the values need to be keccak hashed before they are encoded
            encTypes.append("bytes32")
            hashed_value = keccak(primitive=value)
            encValues.append(hashed_value)
        elif field["type"] in types:
            # This means that this type is a user defined type
            encTypes.append("bytes32")
            hashed_value = keccak(primitive=encodeData(field["type"], types, value))
            encValues.append(hashed_value)
        elif field["type"][-1] == "]":
            # TODO: Replace the above conditionality with Regex for identifying arrays declaration
            raise NotImplementedError("TODO: Arrays currently unimplemented in encodeData")
        else:
            # First checking to see if the individual values can be encoded
            try:
                is_encodable(field["type"], value)
            except:
                raise TypeError(
                    "Received Invalid type `{0}` in the struct `{1}`".format(
                        field["type"],
                        primaryType,
                    )
                )

            # Next see if the data fits the specified encoding type
            if is_encodable(field["type"], value):
                # field["type"] is a valid type and this value corresponds to that type.
                encTypes.append(field["type"])
                encValues.append(value)
            else:
                raise TypeError(
                    "Value of `{0}` ({2}) in the struct `{1}` is of the type `{3}`, but expected "
                    "{4} value".format(
                        field["name"],
                        primaryType,
                        value,
                        type(value),
                        field["type"],
                    )
                )

    return encode_abi(encTypes, encValues)


def validate_has_attribute(attr_name, dict_data):
    if attr_name not in dict_data:
        raise AttributeError(
            "Attribute `{0}` not found in the JSON string".
            format(attr_name)
        )


def validate_types_attribute(structured_data):
    # Check that the data has `types` attribute
    validate_has_attribute("types", structured_data)

    # Check if all the `name` and the `type` attributes in each field of all the
    # `types` attribute are valid (Regex Check)
    for struct_name in structured_data["types"]:
        # Check that `struct_name` is of the type string
        if not isinstance(struct_name, str):
            raise TypeError(
                "Struct Name of `types` attribute should be a string, but got type `{0}`".
                format(type(struct_name))
            )
        for field in structured_data["types"][struct_name]:
            # Check that `field["name"]` is of the type string
            if not isinstance(field["name"], str):
                raise TypeError(
                    "Field Name `{0}` of struct `{1}` should be a string, but got type `{2}`".
                    format(field["name"], struct_name, type(field["name"]))
                )
            # Check that `field["type"]` is of the type string
            if not isinstance(field["type"], str):
                raise TypeError(
                    "Field Type `{0}` of struct `{1}` should be a string, but got type `{2}`".
                    format(field["type"], struct_name, type(field["type"]))
                )
            # Check that field["name"] matches with IDENTIFIER_REGEX
            if not re.match(IDENTIFIER_REGEX, field["name"]):
                raise AttributeError(
                    "Invalid Identifier `{0}` in `{1}`".format(field["name"], struct_name)
                )
            # Check that field["type"] matches with TYPE_REGEX
            if not re.match(TYPE_REGEX, field["type"]):
                raise AttributeError(
                    "Invalid Type `{0}` in `{1}`".format(field["type"], struct_name)
                )


def validate_field_declared_only_once_in_struct(field_name, struct_data, struct_name):
    if len([field for field in struct_data if field["name"] == field_name]) != 1:
        raise AttributeError(
            "Attribute `{0}` not declared or declared more than once in {1}".
            format(field_name, struct_name)
        )


def validate_EIP712Domain_schema(structured_data):
    # Check that the `types` attribute contains `EIP712Domain` schema declaration
    if "EIP712Domain" not in structured_data["types"]:
        raise AttributeError("`EIP712Domain struct` not found in types attribute")
    # Check that the names and types in `EIP712Domain` are what are mentioned in the EIP-712
    # and they are declared only once
    EIP712Domain_data = structured_data["types"]["EIP712Domain"]
    validate_field_declared_only_once_in_struct("name", EIP712Domain_data, "EIP712Domain")
    validate_field_declared_only_once_in_struct("version", EIP712Domain_data, "EIP712Domain")
    validate_field_declared_only_once_in_struct("chainId", EIP712Domain_data, "EIP712Domain")
    validate_field_declared_only_once_in_struct(
        "verifyingContract",
        EIP712Domain_data,
        "EIP712Domain",
    )


def validate_primaryType_attribute(structured_data):
    # Check that `primaryType` attribute is present
    if "primaryType" not in structured_data:
        raise AttributeError("The Structured Data needs to have a `primaryType` attribute")
    # Check that `primaryType` value is a string
    if not isinstance(structured_data["primaryType"], str):
        raise TypeError(
            "Value of attribute `primaryType` should be `string`, but got type `{0}`".
            format(type(structured_data["primaryType"]))
        )
    # Check that the value of `primaryType` is present in the `types` attribute
    if not structured_data["primaryType"] in structured_data["types"]:
        raise AttributeError(
            "The Primary Type `{0}` is not present in the `types` attribute".
            format(structured_data["primaryType"])
        )


def validate_structured_data(structured_data):
    # validate the `types` attribute
    validate_types_attribute(structured_data)
    # validate the `EIP712Domain` attribute of `types` attribute
    validate_EIP712Domain_schema(structured_data)
    # validate the `primaryType` attribute
    validate_primaryType_attribute(structured_data)
    # Check that there is a `domain` attribute in the structured data
    validate_has_attribute("domain", structured_data)
    # Check that there is a `message` attribute in the structured data
    validate_has_attribute("message", structured_data)


def hashStruct(structured_json_string_data, is_domain_separator=False):
    """
    The structured_json_string_data is expected to have the ``types`` attribute and
    the ``primaryType``, ``message``, ``domain`` attribute.
    The ``is_domain_separator`` variable is used to calculate the ``hashStruct`` as
    part of the ``domainSeparator`` calculation.
    """
    structured_data = json.loads(structured_json_string_data)
    validate_structured_data(structured_data)

    types = structured_data["types"]
    if is_domain_separator:
        primaryType = "EIP712Domain"
        data = structured_data["domain"]
    else:
        primaryType = structured_data["primaryType"]
        data = structured_data["message"]
    return keccak(encodeData(primaryType, types, data))


# watch here for updates to signature format:
# https://github.com/ethereum/EIPs/blob/master/EIPS/eip-191.md
# https://github.com/ethereum/EIPs/blob/master/EIPS/eip-712.md
@curry
def signature_wrapper(message, signature_version, version_specific_data):
    if not isinstance(message, bytes):
        raise TypeError("Message is of the type {}, expected bytes".format(type(message)))
    if not isinstance(signature_version, bytes):
        raise TypeError("Signature Version is of the type {}, expected bytes".format(
            type(signature_version))
        )

    if signature_version == PERSONAL_SIGN_VERSION:
        preamble = b'\x19Ethereum Signed Message:\n'
        size = str(len(message)).encode('utf-8')
        return preamble + size + message
    elif signature_version == INTENDED_VALIDATOR_SIGN_VERSION:
        wallet_address = to_bytes(hexstr=version_specific_data)
        if len(wallet_address) != 20:
            raise TypeError("Invalid Wallet Address: {}".format(version_specific_data))
        wrapped_message = b'\x19' + signature_version + wallet_address + message
        return wrapped_message
    elif signature_version == STRUCTURED_DATA_SIGN_VERSION:
        message_string = to_text(primitive=message)
        domainSeparator = hashStruct(message_string, is_domain_separator=True)
        wrapped_message = b'\x19' + signature_version + domainSeparator + hashStruct(message_string)
        return wrapped_message
    else:
        raise NotImplementedError(
            "Currently supported signature versions are: {0}, {1}, {2}. ".
            format(
                '0x' + INTENDED_VALIDATOR_SIGN_VERSION.hex(),
                '0x' + PERSONAL_SIGN_VERSION.hex(),
                '0x' + STRUCTURED_DATA_SIGN_VERSION.hex(),
            ) +
            "But received signature version {}".format('0x' + signature_version.hex())
        )


def hash_of_signed_transaction(txn_obj):
    '''
    Regenerate the hash of the signed transaction object.

    1. Infer the chain ID from the signature
    2. Strip out signature from transaction
    3. Annotate the transaction with that ID, if available
    4. Take the hash of the serialized, unsigned, chain-aware transaction

    Chain ID inference and annotation is according to EIP-155
    See details at https://github.com/ethereum/EIPs/blob/master/EIPS/eip-155.md

    :return: the hash of the provided transaction, to be signed
    '''
    (chain_id, _v) = extract_chain_id(txn_obj.v)
    unsigned_parts = strip_signature(txn_obj)
    if chain_id is None:
        signable_transaction = UnsignedTransaction(*unsigned_parts)
    else:
        extended_transaction = unsigned_parts + [chain_id, 0, 0]
        signable_transaction = ChainAwareUnsignedTransaction(*extended_transaction)
    return signable_transaction.hash()


def extract_chain_id(raw_v):
    '''
    Extracts chain ID, according to EIP-155
    @return (chain_id, v)
    '''
    above_id_offset = raw_v - CHAIN_ID_OFFSET
    if above_id_offset < 0:
        if raw_v in {0, 1}:
            return (None, raw_v + V_OFFSET)
        elif raw_v in {27, 28}:
            return (None, raw_v)
        else:
            raise ValueError("v %r is invalid, must be one of: 0, 1, 27, 28, 35+")
    else:
        (chain_id, v_bit) = divmod(above_id_offset, 2)
        return (chain_id, v_bit + V_OFFSET)


def to_standard_signature_bytes(ethereum_signature_bytes):
    rs = ethereum_signature_bytes[:-1]
    v = to_int(ethereum_signature_bytes[-1])
    standard_v = to_standard_v(v)
    return rs + to_bytes(standard_v)


def to_standard_v(enhanced_v):
    (_chain, chain_naive_v) = extract_chain_id(enhanced_v)
    v_standard = chain_naive_v - V_OFFSET
    assert v_standard in {0, 1}
    return v_standard


def to_eth_v(v_raw, chain_id=None):
    if chain_id is None:
        v = v_raw + V_OFFSET
    else:
        v = v_raw + CHAIN_ID_OFFSET + 2 * chain_id
    return v


def sign_transaction_hash(account, transaction_hash, chain_id):
    signature = account.sign_msg_hash(transaction_hash)
    (v_raw, r, s) = signature.vrs
    v = to_eth_v(v_raw, chain_id)
    return (v, r, s)


def _pad_to_eth_word(bytes_val):
    return bytes_val.rjust(32, b'\0')


def to_bytes32(val):
    return pipe(
        val,
        to_bytes,
        _pad_to_eth_word,
    )


def sign_message_hash(key, msg_hash):
    signature = key.sign_msg_hash(msg_hash)
    (v_raw, r, s) = signature.vrs
    v = to_eth_v(v_raw)
    eth_signature_bytes = to_bytes32(r) + to_bytes32(s) + to_bytes(v)
    return (v, r, s, eth_signature_bytes)
