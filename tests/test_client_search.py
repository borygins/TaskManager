from __future__ import annotations

import unittest

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base, configure_sqlite
from app.models import Client, ClientFunnel, Funnel, User
from app.services.task_service import get_clients_for_distribution, get_funnel_clients


class ClientSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        event.listen(self.engine.sync_engine, "connect", configure_sqlite)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with self.session_factory() as session:
            self.manager = User(
                employee_id="00000001",
                full_name="Менеджер",
                role="manager",
                password_hash="test",
            )
            self.boss = User(
                employee_id="00000002",
                full_name="Руководитель",
                role="boss",
                password_hash="test",
            )
            funnel = Funnel(name="Основная")
            session.add_all([self.manager, self.boss, funnel])
            await session.flush()

            first = Client(
                full_name="Иван Петров",
                company_name="ООО Ромашка",
                tax_number="7701000001",
                assigned_manager_id=self.manager.id,
            )
            second = Client(
                full_name="Анна Сидорова",
                company_name="Северный ветер",
                tax_number="7802000002",
                assigned_manager_id=self.manager.id,
            )
            session.add_all([first, second])
            await session.flush()
            session.add_all(
                [
                    ClientFunnel(client_id=first.id, funnel_id=funnel.id),
                    ClientFunnel(client_id=second.id, funnel_id=funnel.id),
                ]
            )
            await session.commit()
            self.funnel_id = funnel.id

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_funnel_searches_by_inn_name_and_company_case_insensitively(self) -> None:
        cases = {
            "7701": "Иван Петров",
            "иван": "Иван Петров",
            "РОМАШКА": "Иван Петров",
            "северный": "Анна Сидорова",
        }
        async with self.session_factory() as session:
            for query, expected_name in cases.items():
                with self.subTest(query=query):
                    clients = await get_funnel_clients(
                        funnel_id=self.funnel_id,
                        current_user=self.manager,
                        session=session,
                        search_query=query,
                    )
                    self.assertEqual([client.full_name for client in clients], [expected_name])

    async def test_distribution_uses_the_same_search(self) -> None:
        async with self.session_factory() as session:
            clients = await get_clients_for_distribution(session=session, search_query="сидорова")
        self.assertEqual([client.full_name for client in clients], ["Анна Сидорова"])


if __name__ == "__main__":
    unittest.main()
