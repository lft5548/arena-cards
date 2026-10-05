#include <cassert>
#include <string>

#include "server/persistence/mysql_store.h"

int main() {
  using arena::persistence::MysqlStore;
  assert(MysqlStore::normalize_pool_size(0) == 1);
  assert(MysqlStore::normalize_pool_size(1) == 1);
  assert(MysqlStore::normalize_pool_size(4) == 4);
  assert(MysqlStore::normalize_pool_size(8) == 8);
  assert(MysqlStore::normalize_pool_size(9) == 8);

  MysqlStore store;
  assert(store.pool_size() == 1);
  assert(store.active_slots() == 0);
#if !defined(ARENA_WITH_MYSQL) || !ARENA_WITH_MYSQL
  std::string error;
  assert(!store.connect("127.0.0.1", 3306, "", "", "", error));
  assert(error == "Arena was built without MySQL client development files");
#endif
  return 0;
}
