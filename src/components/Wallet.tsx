import React from 'react';
import { connect } from 'react-redux';
import AddSigners from './AddSigners';
import Signers from './Signers';
import { Device, isUnlockedDevice } from '../types'
import { AppState } from '../store';
import { getWallets, selectCandidateDevicesForActiveWallet, selectActiveWallet, addSigner } from '../store/wallet';
import api from '../api'

interface StateProps {
  activeWallet: ReturnType<typeof selectActiveWallet>;
}

interface DispatchProps {
  getWallets: typeof getWallets;
  addSigner: typeof addSigner;
}

interface State {
  deviceBeingAdded: Device | null;
}

type Props = StateProps & DispatchProps;

class Wallet extends React.Component<Props, State> {
  state: State = {
    deviceBeingAdded: null,
  }

  render() {
    const { activeWallet } = this.props;
    if (!activeWallet) {
      return <div>no active wallet</div>
    }
    const { signers } = activeWallet;

    let signersComponent = null;
    if (signers.length) {
      signersComponent = (
        <div>
          <h3 className='text-center'>Signers</h3>
          <Signers signers={signers} />
        </div>
      )
    }

    // "Add Signers" section
    let addSigners = null;
    if (!activeWallet.ready) {
      addSigners = (
        <div>
          <h3 className='text-center'>Add {activeWallet.n - activeWallet.signers.length} More Signers</h3>
          <AddSigners/>
        </div>
      )
    }
    return (
      <div>
        <h2 className='text-center'>{ activeWallet.name } ({activeWallet.m}/{activeWallet.n})</h2>
        {activeWallet.ready && 
          <div className="text-center">Confirmed Balance: {activeWallet.balances.confirmed} BTC</div>
        }
        {activeWallet.balances.unconfirmed > 0 &&
          <div className="text-center">Unconfirmed Balance: {activeWallet.balances.unconfirmed} BTC</div>}
        {signersComponent}
        {addSigners}
        </div>
    )
  }
}
  
const mapStateToProps = (state: AppState) => {
  return {
    activeWallet: selectActiveWallet(state),
  }
}

export default connect(
  mapStateToProps,
  { getWallets },
)(Wallet);