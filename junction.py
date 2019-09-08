import logging
import time
import hwilib
import os.path

from pprint import pprint
from hwilib.serializations import PSBT

from utils import RPC, JSONRPCException, sat_to_btc, btc_to_sat, JunctionError
from disk import write_json_file, read_json_file, ensure_datadir, DATADIR, get_settings, full_path

logger = logging.getLogger(__name__)

ADDRESS_CHUNK = 100

class MultisigWallet:

    def __init__(self, name, m, n, signers, psbt, address_index, export_index):
        # Name of the wallet
        self.name = name
        # Signers required for bitcoin tx
        self.m = m
        # Total signers
        self.n = n
        # Dictionary
        self.signers = signers
        # Hex string
        self.psbt = psbt
        # Depth in HD derivation
        self.address_index = address_index
        # Highest exported address index
        self.export_index = export_index
        # RPC connection to corresponding watch-only Bitcoin Core wallet
        settings = get_settings()  # FIXME
        self.wallet_rpc = RPC(settings, name)
        # RPC connection to Bitcoin Core's default wallet
        self.default_rpc = RPC(settings)

    def ready(self):
        '''All signers present, ready to create PSBT'''
        return len(self.signers) == self.n

    def wallet_file_path(self):
        '''Relative path to wallet file within datadir'''
        return f'wallets/{self.name}.json'

    @classmethod
    def create(cls, name, m, n):
        '''Creates class instance, wallet file, and watch-only Bitcoin Core wallet'''
        # Sanity checks
        if m > n:
            raise JunctionError(f"\"m\" ({m}) must be no larger than \"n\" ({n})")
        if m < 1:
            raise JunctionError(f"\"m\" ({m}) must be larger than 0")
        if n > 5:
            raise JunctionError(f"\"n\" ({n}) cannot exceed 5")

        # Make sure datadir exists
        ensure_datadir()

        # MultisigWallet instance
        wallet = cls(name, m, n, [], None, 0, 0)

        # Never overwrite existing wallet files
        # FIXME: full_path probably shouldn't appear in this file?
        wallet_file_path = full_path(f'wallets/{name}.json')
        if os.path.exists(wallet_file_path):
            raise JunctionError(f'"{wallet_file_path}" wallet file already exists')

        # Create a watch-only Bitcoin Core wallet
        wallet.ensure_watchonly()

        # Save a copy of wallet to disk
        wallet.save()

        # Return instance
        return wallet

    @classmethod
    def open(cls, wallet_name):
        '''Initialize this class from an existing wallet file'''
        relative_path = f'wallets/{wallet_name}.json'
        wallet_dict = read_json_file(relative_path)
        wallet = cls.from_dict(wallet_dict)
        wallet.ensure_watchonly()
        logger.info(f"Opened wallet from {relative_path}")
        return wallet

    def save(self):
        '''Save wallet file to disk'''
        data = self.to_dict()  
        relative_path = self.wallet_file_path()
        write_json_file(data, relative_path)
        logger.info(f"Saved wallet to {relative_path}")

    @classmethod
    def from_dict(cls, d):
        '''Create class instance from dictionary'''
        if d["psbt"]:
            psbt = hwilib.serializations.PSBT()
            psbt.deserialize(d["psbt"])
            d["psbt"] = psbt
        return cls(**d)
        
    def to_dict(self):
        '''Represent instance as a dictionary'''
        return {
            "name": self.name,
            "m": self.m,
            "n": self.n,
            "signers": self.signers,
            "psbt": self.psbt.serialize() if self.psbt else "",
            "address_index": self.address_index,
            "export_index": self.export_index,
        }

    def add_signer(self, name, fingerprint, xpub, derivation_path):
        '''Add a signer to multisig wallet'''
        if self.ready():
            raise JunctionError(f'Already have {len(self.signers)} of {self.n} required signers')

        # Check if name used before
        if name in [signer["name"] for signer in self.signers]:
            raise JunctionError(f'Name "{name}" already taken')

        # Check if fingerprint used before
        if fingerprint in [signer["fingerprint"] for signer in self.signers]:
            raise JunctionError(f'Fingerprint "{fingerprint}" already used')

        self.signers.append({"name": name, "fingerprint": fingerprint, "xpub": xpub, 'derivation_path': derivation_path})
        logger.info(f"Registered signer \"{name}\"")

        # Export next chunk watch-only addresses to Bitcoin Core if we're done adding signers
        if self.ready():
            self.export_watchonly()
        self.save()

    def remove_signer(signer_name):
        '''Remove signer from multisig wallet'''
        raise NotImplementedError()

    def descriptor(self):
        '''Descriptor for shared multisig addresses'''
        # TODO: consider using HWI's Descriptor class
        path_prefix = "/44h/1h/0h"
        # TODO: add change parameter and inject here
        path_suffix = "/0/*"
        xpubs = [f'[{signer["fingerprint"]}{path_prefix}]{signer["xpub"]}{path_suffix}' 
                for signer in self.signers]
        xpubs = ",".join(xpubs)
        descriptor = f"sh(multi({self.m},{xpubs}))"
        logger.info(f"Exporting descriptor: {descriptor}")
        # validates and appends checksum
        with self.wallet_rpc as wallet_rpc:
            r = wallet_rpc.getdescriptorinfo(descriptor)
        return r['descriptor']

    def address(self):
        '''Derive next BIP67-compliant receiving address'''
        if not self.ready():
            raise JunctionError(f'{self.n} signers required, {len(self.signers)} registered')
        if self.address_index > self.export_index:
            self.export_watchonly()
        with self.wallet_rpc as wallet_rpc:            
            address = wallet_rpc.deriveaddresses(
                self.descriptor(), 
                [self.address_index, self.address_index + 1])[0]
        self.address_index += 1

        # Hackily skip BIP67 (sorted multisig pubkeys) violations
        # (ColdCard demands BIP67 compliance, descriptor language doesn't support)
        with self.wallet_rpc as wallet_rpc:
            ai = self.wallet_rpc.getaddressinfo(address)
        assert ai['iswatchonly'] is True, 'Bitcoin Core gave us non-watch-only address'
        if ai['pubkeys'] != sorted(ai['pubkeys']):
            return self.address()
            
        self.save()
        return address

    def ensure_watchonly(self):
        # Create watch-only Bitcoin Core wallet
        watch_only_name = self.watchonly_name()
        try:
            with self.default_rpc as default_rpc:
                bitcoin_wallets = default_rpc.listwallets()
        except Exception as e:
            # handle_exception(e)  # Janky: don't bring flask into this
            # probably should just let RPC exception bubble up and catch this in caller
            raise JunctionError(e)
        if watch_only_name not in bitcoin_wallets:
            try:
                with self.default_rpc as default_rpc:
                    default_rpc.loadwallet(watch_only_name)
                logger.info(f"Loaded watch-only Bitcoin Core wallet \"{watch_only_name}\"")
            except JSONRPCException as e:
                try:
                    with self.default_rpc as default_rpc:
                        default_rpc.createwallet(watch_only_name, True)
                    logger.info(f"Created watch-only Bitcoin Core wallet \"{watch_only_name}\"")
                except JSONRPCException as e:
                    raise JunctionError("Couldn't establish watch-only Bitcoin Core wallet")

    def watchonly_name(self):
        # maybe add a "junction_" prefix or something?
        return self.name

    def export_watchonly(self):
        '''Export addresses to Bitcoin Core watch-only wallet'''
        logger.info("Starting watch-only export")
        new_export_index = self.export_index + ADDRESS_CHUNK
        with self.wallet_rpc as wallet_rpc:
            wallet_rpc.importmulti([{
                "desc": self.descriptor(),
                # 24 hours just in case
                "timestamp": int(time.time()) - 60*60*24,
                # FIXME: is this inclusive? if we we're overlapping 1 ever time ...
                "range": [self.export_index, new_export_index],
                "watchonly": True,
                # Bitcoin Core cannot import P2SH/P2WSH: https://bitcoin.stackexchange.com/a/89118/85335
                "keypool": False,
                "internal": False,
            }])
        self.export_index = new_export_index
        self.save()
        logger.info("Finished watch-only export")

    def create_psbt(self, recipient, satoshis):
        '''Create a new PSBT paying single recipient'''
        if self.psbt:
            raise JunctionError('PSBT already present')
        change_address = self.address()
        with self.wallet_rpc as wallet_rpc:
            raw_psbt = wallet_rpc.walletcreatefundedpsbt(
                # let Bitcoin Core choose inputs
                [],
                # Outputs
                [{recipient: sat_to_btc(satoshis)}],
                # Locktime
                0, 
                {
                    # Include watch-only outputs
                    "includeWatching": True,
                    # Provide change address b/c Core can't generate it
                    "changeAddress": change_address,
                },
                # Include BIP32 derivation paths in the PSBT
                True,
            )['psbt']
        # Serialize and save
        self.psbt = hwilib.serializations.PSBT()
        self.psbt.deserialize(raw_psbt)
        self.save()

    def remove_psbt(self):
        self.psbt = None

    def decode_psbt(self):
        '''Fetch Bitcoin Core psbt deserialization if it exists'''
        with self.wallet_rpc as wallet_rpc:
            if self.psbt:
                return wallet_rpc.decodepsbt(self.psbt.serialize())
            else:
                return None

    def signing_complete(self):
        '''Check that we have m signatures'''
        return self.m == len(self.psbt.inputs[0].partial_sigs)

    def broadcast(self):
        '''Finalize and broadcast psbt to network'''
        psbt_hex = self.psbt.serialize()
        with self.wallet_rpc as wallet_rpc:
            tx_hex = wallet_rpc.finalizepsbt(psbt_hex)["hex"]
            return wallet_rpc.sendrawtransaction(tx_hex)
    
    def balances(self):
        '''(unconfirmed, confirmed) balances tuple'''
        # try to use new getbalances rpc (available in bitcoin core master branch)
        try:
            with self.wallet_rpc as wallet_rpc:
                balances = wallet_rpc.getbalances()
            if 'watchonly' in balances:
                watchonly = balances['watchonly']
                return watchonly['untrusted_pending'], watchonly['trusted']
            else:
                return 0, 0
        # fall back to counting ourselves
        except:
            unconfirmed_balance = 0
            confirmed_balance = 0
            with self.wallet_rpc as wallet_rpc:
                unspent = wallet_rpc.listunspent(0, 9999999, [], True)
            for u in unspent:
                if u['confirmations'] > 0:
                    confirmed_balance += u['amount']
                else:
                    unconfirmed_balance += u['amount']
            return unconfirmed_balance, confirmed_balance
