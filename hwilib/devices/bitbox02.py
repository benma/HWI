from typing import (
    cast,
    Any,
    Callable,
    Dict,
    Optional,
    Union,
    Tuple,
    List,
    Sequence,
    TypeVar,
)
from binascii import unhexlify
import struct
import builtins
import sys
import os
import json
from pathlib import Path
from functools import wraps

from ..hwwclient import HardwareWalletClient
from ..serializations import (
    PSBT,
    CTxOut,
    is_p2pkh,
    is_p2sh,
    is_p2wpkh,
    is_p2wsh,
    is_witness,
    ser_uint256,
    ser_sig_der,
)
from ..errors import (
    HWWError,
    ActionCanceledError,
    BadArgumentError,
    DeviceNotReadyError,
    UnavailableActionError,
    DEVICE_NOT_INITIALIZED,
    handle_errors,
    common_err_msgs,
)

import hid  # type: ignore

from .trezorlib.tools import parse_path

from bitbox02 import util
from bitbox02 import bitbox02
from bitbox02.communication import (
    devices,
    u2fhid,
    FirmwareVersionOutdatedException,
    Bitbox02Exception,
    UserAbortException,
    HARDENED,
    ERR_GENERIC,
)

from bitbox02.communication.bitbox_api_protocol import (
    Platform,
    BitBox02Edition,
    BitBoxNoiseConfig,
)


class KeypathError(UnavailableActionError):
    def __init__(self, keypath: str, is_testnet: bool):
        """
        Keypath error exception with formatting and docs.
        """
        network = "testnet" if is_testnet else "mainnet"
        msg = "The BitBox02 does not support the keypath {} on {}. Supported keypaths are:\n".format(
            keypath, network
        )
        msg += "m/49'/0'/<account'> for p2wpkh-p2sh\n"
        msg += "m/84'/0'/<account'> for p2wpkh\n"
        msg += "m/48'/0'/<account'>/2' for p2wsh multisig\n"
        msg += "account can be between 0' and 99'\n"
        msg += "For address keypaths, append /0/<address index> for a receive and /1/<change index> for a change address."
        super().__init__(msg)


ERR_INVALID_INPUT = 101

PURPOSE_P2WPKH_P2SH = 49 + HARDENED
PURPOSE_P2WPKH = 84 + HARDENED
PURPOSE_MULTISIG_P2WSH = 48 + HARDENED

# External GUI tools using hwi.py as a command line tool to integrate hardware wallets usually do
# not have an actual terminal for IO.
_using_external_gui = not sys.stdout.isatty()
if _using_external_gui:
    _unpaired_errmsg = "Device not paired yet. Please pair using the BitBoxApp, then close the BitBoxApp and try again."
else:
    _unpaired_errmsg = "Device not paired yet. Please use any subcommand to pair"


class SilentNoiseConfig(util.BitBoxAppNoiseConfig):
    """
    Used during `enumerate()`. Raises an exception if the device is unpaired.
    Attestation check is silent.

    Rationale: enumerate() should not show any dialogs.
    """

    def show_pairing(self, code: str, device_response: Callable[[], bool]) -> bool:
        raise DeviceNotReadyError(_unpaired_errmsg)

    def attestation_check(self, result: bool) -> None:
        pass


class CLINoiseConfig(util.BitBoxAppNoiseConfig):
    """ Noise pairing and attestation check handling in the terminal (stdin/stdout) """

    def show_pairing(self, code: str, device_response: Callable[[], bool]) -> bool:
        if _using_external_gui:
            # The user can't see the pairing in the terminal. The
            # output format is also not appropriate for parsing by
            # external tools doing inter process communication using
            # stdin/stdout. For now, we direct the user to pair in the
            # BitBoxApp instead.
            raise DeviceNotReadyError(_unpaired_errmsg)

        print("Please compare and confirm the pairing code on your BitBox02:")
        print(code)
        if not device_response():
            return False
        return input("Accept pairing? [y]/n: ").strip() != "n"

    def attestation_check(self, result: bool) -> None:
        if result:
            sys.stderr.write("BitBox02 attestation check PASSED\n")
        else:
            sys.stderr.write("BitBox02 attestation check FAILED\n")
            sys.stderr.write(
                "Your BitBox02 might not be genuine. Please contact support@shiftcrypto.ch if the problem persists.\n"
            )


def _keypath_check_account(bip44_account: int) -> bool:
    return HARDENED <= bip44_account <= HARDENED + 99


def enumerate(password: str = "") -> List[Dict[str, object]]:
    """
    Enumerate all BitBox02 devices. Bootloaders excluded.
    """
    result = []
    for device_info in devices.get_any_bitbox02s():
        path = device_info["path"].decode()
        client = Bitbox02Client(path)
        client.set_noise_config(SilentNoiseConfig())
        version, platform, edition, unlocked = bitbox02.BitBox02.get_info(
            client.transport
        )
        if platform != Platform.BITBOX02:
            client.close()
            continue
        if edition not in (BitBox02Edition.MULTI, BitBox02Edition.BTCONLY):
            client.close()
            continue

        assert isinstance(edition, BitBox02Edition)

        d_data = {
            "type": "bitbox02",
            "path": path,
            "model": {
                BitBox02Edition.MULTI: "bitbox02_multi",
                BitBox02Edition.BTCONLY: "bitbox02_btconly",
            }[edition],
            "needs_pin_sent": False,
            "needs_passphrase_sent": False,
        }

        with handle_errors(common_err_msgs["enumerate"], d_data):
            if not unlocked:
                raise DeviceNotReadyError(
                    "Please load wallet to unlock."
                    if _using_external_gui
                    else "Please use any subcommand to unlock"
                )
            bb02 = client.init()
            info = bb02.device_info()
            if not info["initialized"]:
                raise HWWError("Not initialized", DEVICE_NOT_INITIALIZED)
            d_data["fingerprint"] = client.get_master_fingerprint_hex()

        result.append(d_data)

        client.close()
    return result


T = TypeVar("T", bound=Callable[..., Any])


def bitbox02_exception(f: T) -> T:
    """
    Maps bitbox02 library exceptions into a HWI exceptions.
    """

    @wraps(f)
    def func(*args, **kwargs):  # type: ignore
        """ Wraps f, mapping exceptions. """
        try:
            return f(*args, **kwargs)
        except UserAbortException:
            raise ActionCanceledError("{} canceled".format(f.__name__))
        except Bitbox02Exception as exc:
            if exc.code in (ERR_GENERIC, ERR_INVALID_INPUT):
                raise BadArgumentError("invalid input")
            raise exc
        except FirmwareVersionOutdatedException as exc:
            raise DeviceNotReadyError(exc.message)

    return cast(T, func)


# This class extends the HardwareWalletClient for BitBox02 specific things
class Bitbox02Client(HardwareWalletClient):
    def __init__(self, path: str, password: str = "", expert: bool = False) -> None:
        """
        Initializes a new BitBox02 client instance.
        """
        super().__init__(path, password=password, expert=expert)
        if password != "":
            raise BadArgumentError(
                "The BitBox02 does not accept a passphrase from the host. Please enable the passphrase option and enter the passphrase on the device during unlock."
            )

        hid_device = hid.device()
        hid_device.open_path(path.encode())
        self.transport = u2fhid.U2FHid(hid_device)
        self.device_path = path

        # use self.init() to access self.bb02.
        self.bb02: Optional[bitbox02.BitBox02] = None

        self.noise_config: BitBoxNoiseConfig = CLINoiseConfig()

    def set_noise_config(self, noise_config: BitBoxNoiseConfig) -> None:
        self.noise_config = noise_config

    def init(self) -> bitbox02.BitBox02:
        if self.bb02 is not None:
            return self.bb02

        for device_info in devices.get_any_bitbox02s():
            if device_info["path"].decode() == self.device_path:
                bb02 = bitbox02.BitBox02(
                    transport=self.transport,
                    device_info=device_info,
                    noise_config=self.noise_config,
                )
                try:
                    bb02.check_min_version()
                except FirmwareVersionOutdatedException as exc:
                    sys.stderr.write("WARNING: {}\n".format(exc))
                    raise
            self.bb02 = bb02
            return bb02
        raise Exception(
            "Could not find the hid device info for path {}".format(self.device_path)
        )

    def close(self) -> None:
        self.transport.close()

    def get_master_fingerprint_hex(self) -> str:
        """
        HWI by default retrieves the fingerprint at m/ by getting the xpub at m/0', which contains the parent fingerprint.
        The BitBox02 does not support querying arbitrary keypaths, but has an api call return the fingerprint at m/.
        """
        bb02 = self.init()
        if not bb02.device_info()["initialized"]:
            raise UnavailableActionError("Not initialized")
        return bb02.root_fingerprint().hex()

    def prompt_pin(self) -> Dict[str, Union[bool, str, int]]:
        raise UnavailableActionError(
            "The BitBox02 does not need a PIN sent from the host"
        )

    def send_pin(self) -> Dict[str, Union[bool, str, int]]:
        raise UnavailableActionError(
            "The BitBox02 does not need a PIN sent from the host"
        )

    def _get_coin(self) -> bitbox02.btc.BTCCoin:
        if self.is_testnet:
            return bitbox02.btc.TBTC
        return bitbox02.btc.BTC

    def _get_xpub(self, keypath: List[int]) -> str:
        expected_coin = 1 + HARDENED if self.is_testnet else 0 + HARDENED
        if len(keypath) == 3:
            # singlesig
            purpose, coin, account = keypath
            if coin != expected_coin or not _keypath_check_account(account):
                raise ValueError()
            try:
                # Actually we want to show ypub... or zpub... for segwit-p2sh or native segwit,
                # but some downstream projects using HWI can just parse xpub and tpub.
                # Maybe we can change it to Electrum compatible xpub formats someday.
                xpub_type = {
                    False: {
                        PURPOSE_P2WPKH_P2SH: bitbox02.btc.BTCPubRequest.XPUB,  # bitbox02.btc.BTCPubRequest.YPUB,
                        PURPOSE_P2WPKH: bitbox02.btc.BTCPubRequest.XPUB,  # bitbox02.btc.BTCPubRequest.ZPUB,
                    },
                    True: {
                        PURPOSE_P2WPKH_P2SH: bitbox02.btc.BTCPubRequest.TPUB,  # bitbox02.btc.BTCPubRequest.UPUB,
                        PURPOSE_P2WPKH: bitbox02.btc.BTCPubRequest.TPUB,  # bitbox02.btc.BTCPubRequest.VPUB,
                    },
                }[self.is_testnet][purpose]
            except KeyError:
                raise ValueError()
        elif len(keypath) == 4:
            # multisig
            purpose, coin, account, script_type = keypath
            if (
                purpose != PURPOSE_MULTISIG_P2WSH
                or coin != expected_coin
                or not _keypath_check_account(account)
                or script_type != 2 + HARDENED
            ):
                raise ValueError()
            if self.is_testnet:
                xpub_type = (
                    bitbox02.btc.BTCPubRequest.TPUB
                )  # bitbox02.btc.BTCPubRequest.CAPITAL_VPUB
            else:
                xpub_type = (
                    bitbox02.btc.BTCPubRequest.XPUB
                )  # bitbox02.btc.BTCPubRequest.CAPITAL_ZPUB
        else:
            raise ValueError()

        return self.init().btc_xpub(
            keypath, coin=self._get_coin(), xpub_type=xpub_type, display=False
        )

    def get_pubkey_at_path(self, bip32_path: str) -> Dict[str, str]:
        path_uint32s = parse_path(bip32_path)
        try:
            xpub = self._get_xpub(path_uint32s)
        except ValueError:
            raise KeypathError(bip32_path, self.is_testnet)
        return {"xpub": xpub}

    def _check_address_keypath_simple(
        self, bip32_path: str, expected_purpose: int
    ) -> bool:
        path_uint32s = parse_path(bip32_path)
        if len(path_uint32s) != 5:
            return False
        purpose, coin, account, change, address = path_uint32s
        if purpose != expected_purpose:
            return False
        expected_coin = 1 + HARDENED if self.is_testnet else 0 + HARDENED
        if coin != expected_coin:
            return False
        if not _keypath_check_account(account):
            return False
        if change not in (0, 1):
            return False
        if not (0 <= address <= 9999):
            return False
        return True

    @bitbox02_exception
    def display_address(
        self,
        bip32_path: str,
        p2sh_p2wpkh: bool,
        bech32: bool,
        redeem_script: Optional[str] = None,
    ) -> Dict[str, str]:
        if redeem_script:
            raise NotImplementedError("BitBox02 multisig not integrated into HWI yet")

        keypath_exception = KeypathError(bip32_path, self.is_testnet)

        if p2sh_p2wpkh:
            script_config = bitbox02.btc.BTCScriptConfig(
                simple_type=bitbox02.btc.BTCScriptConfig.P2WPKH_P2SH
            )
            if not self._check_address_keypath_simple(bip32_path, PURPOSE_P2WPKH_P2SH):
                raise keypath_exception
        elif bech32:
            script_config = bitbox02.btc.BTCScriptConfig(
                simple_type=bitbox02.btc.BTCScriptConfig.P2WPKH
            )
            if not self._check_address_keypath_simple(bip32_path, PURPOSE_P2WPKH):
                raise keypath_exception
        else:
            raise UnavailableActionError(
                "The BitBox02 does not support legacy p2pkh addresses"
            )
        address = self.init().btc_address(
            parse_path(bip32_path),
            coin=self._get_coin(),
            script_config=script_config,
            display=True,
        )
        return {"address": address}

    @bitbox02_exception
    def sign_tx(self, psbt: PSBT) -> Dict[str, str]:
        SCRIPT_CONFIG_INDEX_P2WPKH = 0
        SCRIPT_CONFIG_INDEX_P2WPKH_P2SH = 1

        def find_our_key(
            keypaths: Dict[bytes, Sequence[int]]
        ) -> Tuple[Optional[bytes], Optional[Sequence[int]]]:
            """
            Keypaths is a map of pubkey to hd keypath, where the first element in the keypath is the master fingerprint. We attempt to find the key which belongs to the BitBox02 by matching the fingerprint, and then matching the pubkey.
            Returns the pubkey and the keypath, without the fingerprint.
            """
            for pubkey, keypath_with_fingerprint in keypaths.items():
                fp, keypath = keypath_with_fingerprint[0], keypath_with_fingerprint[1:]
                # Cheap check if the key is ours.
                if fp != master_fp:
                    continue

                # Expensive check if the key is ours.
                # TODO: check for fingerprint collision
                # keypath_account = keypath[:-2]

                return pubkey, keypath
            return None, None

        def get_simple_type(
            output: CTxOut, redeem_script: bytes
        ) -> bitbox02.btc.BTCScriptConfig.SimpleType:
            if is_p2pkh(output.scriptPubKey):
                raise BadArgumentError(
                    "The BitBox02 does not support legacy p2pkh scripts"
                )
            if is_p2wpkh(output.scriptPubKey):
                return bitbox02.btc.BTCScriptConfig.P2WPKH
            if output.is_p2sh() and is_p2wpkh(redeem_script):
                return bitbox02.btc.BTCScriptConfig.P2WPKH_P2SH
            raise BadArgumentError(
                "Input script type not recognized of input {}.".format(input_index)
            )

        master_fp = struct.unpack("<I", unhexlify(self.get_master_fingerprint_hex()))[0]

        inputs: List[bitbox02.BTCInputType] = []

        bip44_account = None

        # One pubkey per input. The pubkey identifies the key per input with which we sign. There
        # must be exactly one pubkey per input that belongs to the BitBox02.
        found_pubkeys: List[bytes] = []

        for input_index, (psbt_in, tx_in) in builtins.enumerate(
            zip(psbt.inputs, psbt.tx.vin)
        ):
            if psbt_in.sighash and psbt_in.sighash != 1:
                raise BadArgumentError(
                    "The BitBox02 only supports SIGHASH_ALL. Found sighash: {}".format(
                        psbt_in.sighash
                    )
                )

            utxo = None
            prevtx = None

            # psbt_in.witness_utxo was originally used for segwit utxo's, but since it was
            # discovered that the amounts are not correctly committed to in the segwit sighash, the
            # full prevtx (non_witness_utxo) is supplied for both segwit and non-segwit inputs.
            # See
            # - https://medium.com/shiftcrypto/bitbox-app-firmware-update-6-2020-c70f733a5330
            # - https://blog.trezor.io/details-of-firmware-updates-for-trezor-one-version-1-9-1-and-trezor-model-t-version-2-3-1-1eba8f60f2dd.
            # - https://github.com/zkSNACKs/WalletWasabi/pull/3822
            # The BitBox02 for now requires the prevtx, at least until Taproot activates.

            if psbt_in.non_witness_utxo:
                if tx_in.prevout.hash != psbt_in.non_witness_utxo.sha256:
                    raise BadArgumentError(
                        "Input {} has a non_witness_utxo with the wrong hash".format(
                            input_index
                        )
                    )
                utxo = psbt_in.non_witness_utxo.vout[tx_in.prevout.n]
                prevtx = psbt_in.non_witness_utxo
            elif psbt_in.witness_utxo:
                utxo = psbt_in.witness_utxo
            if utxo is None:
                raise BadArgumentError("No utxo found for input {}".format(input_index))
            if prevtx is None:
                raise BadArgumentError(
                    "Previous transaction missing for input {}".format(input_index)
                )

            found_pubkey, keypath = find_our_key(psbt_in.hd_keypaths)
            if not found_pubkey:
                raise BadArgumentError("No key found for input {}".format(input_index))
            assert keypath is not None
            found_pubkeys.append(found_pubkey)

            # TOOD: validate keypath

            if bip44_account is None:
                bip44_account = keypath[2]
            elif bip44_account != keypath[2]:
                raise BadArgumentError(
                    "The bip44 account index must be the same for all inputs and changes"
                )

            simple_type = get_simple_type(utxo, psbt_in.redeem_script)

            script_config_index_map = {
                bitbox02.btc.BTCScriptConfig.P2WPKH: 0,
                bitbox02.btc.BTCScriptConfig.P2WPKH_P2SH: 1,
            }

            inputs.append(
                {
                    "prev_out_hash": ser_uint256(tx_in.prevout.hash),
                    "prev_out_index": tx_in.prevout.n,
                    "prev_out_value": utxo.nValue,
                    "sequence": tx_in.nSequence,
                    "keypath": keypath,
                    "script_config_index": script_config_index_map[simple_type],
                    "prev_tx": {
                        "version": prevtx.nVersion,
                        "locktime": prevtx.nLockTime,
                        "inputs": [
                            {
                                "prev_out_hash": ser_uint256(prev_in.prevout.hash),
                                "prev_out_index": prev_in.prevout.n,
                                "signature_script": prev_in.scriptSig,
                                "sequence": prev_in.nSequence,
                            }
                            for prev_in in prevtx.vin
                        ],
                        "outputs": [
                            {
                                "value": prev_out.nValue,
                                "pubkey_script": prev_out.scriptPubKey,
                            }
                            for prev_out in prevtx.vout
                        ],
                    },
                }
            )

        outputs: List[bitbox02.BTCOutputType] = []
        for output_index, (psbt_out, tx_out) in builtins.enumerate(
            zip(psbt.outputs, psbt.tx.vout)
        ):
            _, keypath = find_our_key(psbt_out.hd_keypaths)
            is_change = keypath and keypath[-2] == 1
            if is_change:
                assert keypath is not None
                simple_type = get_simple_type(tx_out, psbt_out.redeem_script)
                outputs.append(
                    bitbox02.BTCOutputInternal(
                        keypath=keypath,
                        value=tx_out.nValue,
                        script_config_index=script_config_index_map[simple_type],
                    )
                )
            else:
                if tx_out.is_p2pkh():
                    output_type = bitbox02.btc.P2PKH
                    output_hash = tx_out.scriptPubKey[3:23]
                elif is_p2wpkh(tx_out.scriptPubKey):
                    output_type = bitbox02.btc.P2WPKH
                    output_hash = tx_out.scriptPubKey[2:]
                elif tx_out.is_p2sh():
                    output_type = bitbox02.btc.P2SH
                    output_hash = tx_out.scriptPubKey[2:22]
                elif is_p2wsh(tx_out.scriptPubKey):
                    output_type = bitbox02.btc.P2WSH
                    output_hash = tx_out.scriptPubKey[2:]
                else:
                    raise BadArgumentError(
                        "Output type not recognized of output {}".format(output_index)
                    )

                outputs.append(
                    bitbox02.BTCOutputExternal(
                        output_type=output_type,
                        output_hash=output_hash,
                        value=tx_out.nValue,
                    )
                )

        assert bip44_account is not None

        bip44_network = 1 + HARDENED if self.is_testnet else 0 + HARDENED
        sigs = self.init().btc_sign(
            bitbox02.btc.TBTC if self.is_testnet else bitbox02.btc.BTC,
            [
                bitbox02.btc.BTCScriptConfigWithKeypath(
                    script_config=bitbox02.btc.BTCScriptConfig(
                        simple_type=bitbox02.btc.BTCScriptConfig.P2WPKH
                    ),
                    keypath=[84 + HARDENED, bip44_network, bip44_account],
                ),
                bitbox02.btc.BTCScriptConfigWithKeypath(
                    script_config=bitbox02.btc.BTCScriptConfig(
                        simple_type=bitbox02.btc.BTCScriptConfig.P2WPKH_P2SH
                    ),
                    keypath=[49 + HARDENED, bip44_network, bip44_account],
                ),
            ],
            inputs=inputs,
            outputs=outputs,
            locktime=psbt.tx.nLockTime,
            version=psbt.tx.nVersion,
        )

        for (_, sig), pubkey, psbt_in in zip(sigs, found_pubkeys, psbt.inputs):
            r, s = sig[:32], sig[32:64]
            # ser_sig_der() adds SIGHASH_ALL
            psbt_in.partial_sigs[pubkey] = ser_sig_der(r, s)

        return {"psbt": psbt.serialize()}

    def sign_message(self, message: str, bip32_path: str) -> Dict[str, str]:
        raise UnavailableActionError("The BitBox02 does not support 'signmessage'")

    @bitbox02_exception
    def toggle_passphrase(self) -> Dict[str, Union[bool, str, int]]:
        bb02 = self.init()
        info = bb02.device_info()
        if info["mnemonic_passphrase_enabled"]:
            bb02.disable_mnemonic_passphrase()
        else:
            bb02.enable_mnemonic_passphrase()
        return {"success": True}

    @bitbox02_exception
    def setup_device(
        self, label: str = "", passphrase: str = ""
    ) -> Dict[str, Union[bool, str, int]]:
        if passphrase:
            raise UnavailableActionError(
                "Passphrase not needed when setting up a BitBox02."
            )

        bb02 = self.init()
        if bb02.device_info()["initialized"]:
            raise UnavailableActionError("The BitBox02 must be wiped before setup.")

        if label:
            bb02.set_device_name(label)
        if not bb02.set_password():
            return {"success": False}
        return {"success": bb02.create_backup()}

    @bitbox02_exception
    def wipe_device(self) -> Dict[str, Union[bool, str, int]]:
        return {"success": self.init().reset()}

    @bitbox02_exception
    def backup_device(
        self, label: str = "", passphrase: str = ""
    ) -> Dict[str, Union[bool, str, int]]:
        if label or passphrase:
            raise UnavailableActionError(
                "Label/passphrase not needed when exporting mnemonic from the BitBox02."
            )

        return {"success": self.init().show_mnemonic()}

    @bitbox02_exception
    def restore_device(
        self, label: str = "", word_count: int = 24
    ) -> Dict[str, Union[bool, str, int]]:
        bb02 = self.init()
        if bb02.device_info()["initialized"]:
            raise UnavailableActionError("The BitBox02 must be wiped before restore.")

        if label:
            bb02.set_device_name(label)

        return {"success": bb02.restore_from_mnemonic()}
