"""A household: the actor whose month-opened reply is the ordered actions it requests."""

from abc import abstractmethod
from collections.abc import Sequence

from more_itertools import one

from finance.augur.sim.accounting import AccountStatement
from finance.augur.sim.actions import Action
from finance.augur.sim.actor import Actor, MonthOpened, Statement
from finance.augur.sim.claims import Due
from finance.augur.sim.held_bonds import BondStatement
from finance.augur.sim.holdings import PositionStatement
from finance.augur.sim.ids import AgentId
from finance.augur.sim.managed import TlhStatement
from finance.augur.sim.market_path import MarketStatement
from finance.augur.sim.money import checked_count
from finance.augur.sim.observations import Claim, Observation
from finance.augur.sim.results import Receipt

type Mail = (
    MonthOpened | MarketStatement | AccountStatement | PositionStatement | BondStatement | TlhStatement | Due | Receipt
)


def assemble(agent_id: AgentId, month: int, mail: Sequence[Mail]) -> Observation:
    """The flat view a policy reads, built from one month's statements, dues and receipts."""

    def statement[S: Statement](kind: type[S]) -> S:
        statement = one(
            (message for message in mail if isinstance(message, kind)),
            too_short=ValueError(f"no {kind.__name__} for {agent_id!r} in month {month}"),
            too_long=ValueError(f"several {kind.__name__} for {agent_id!r} in month {month}"),
        )
        if statement.month != month:
            raise ValueError(f"{kind.__name__} is for month {statement.month}, not {month}")
        return statement

    market = statement(MarketStatement)
    accounts = statement(AccountStatement)
    positions = statement(PositionStatement)
    bonds = statement(BondStatement)
    tlh = statement(TlhStatement)
    return Observation(
        agent_id=agent_id,
        month=month,
        cpi=market.cpi,
        cash=checked_count(sum(amount for _, amount in accounts.accounts), "actor cash"),
        public_holdings=checked_count(sum(position.value for position in positions.positions), "public value"),
        accounts=accounts.accounts,
        holding_pools=positions.pools,
        public_positions=positions.positions,
        held_bonds=bonds.bonds,
        tlh_portfolios=tlh.portfolios,
        claims=tuple(message for message in mail if isinstance(message, Claim)),
        previous_receipts=tuple(message for message in mail if isinstance(message, Receipt)),
    )


class EconomicAgent(Actor[Mail, Action]):
    """Subclass with the experiment's real state (spending tier, memory, parameters).

    Statements, dues and last month's receipts arrive when the month opens and are
    kept until `MonthOpened`, when `decide` sees them assembled and returns the ordered
    actions the household requests. A rejected action stops the path and no later
    `decide` call follows on it.
    """

    def __init__(self, agent_id: AgentId) -> None:
        super().__init__(agent_id)
        self.mail: list[Mail] = []

    def handle(self, message: Mail) -> list[Action]:
        if not isinstance(message, MonthOpened):
            self.mail.append(message)
            return []
        view = assemble(self.agent_id, message.month, self.mail)
        self.mail.clear()
        return self.decide(view)

    @abstractmethod
    def decide(self, observation: Observation) -> list[Action]: ...
