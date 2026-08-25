# The eight kitchens

Every kitchen is a fixed hand-authored ASCII grid. Each contains exactly one of each of the nine
stations and exactly four spawns, so the only thing that differs between variants is geometry.

| char | object | char | object |
| --- | --- | --- | --- |
| `#` | a **counter** (holds one item) | `.` | floor |
| `V` | veg station | `M` | meat station |
| `L` | plate stack | `X` | chopping board |
| `O` | the pot | `F` | the fryer |
| `S` | the pass | `W` | the sink |
| `B` | the order board | `@` | a spawn |

## `open-kitchen` -- the control

13x9, one counter island in the middle; everything is reachable both ways round. No forced
hand-off, no choke.

```
#####V#M#####
#...........#
#..@.....@..#
#B..#####..X#
#...#####...#
#L..#####..O#
#..@.....@..#
#...........#
#####W#S#F###
```

## `cramped` -- personal space

9x7, a 7x5 interior for four cogs; every station is on a wall and two cogs cannot pass without
one giving way. This is also the certification and smoke fixture, because it is the smallest.

```
###V#M###
#...@...#
#B.....X#
#.@...@.#
#L.....O#
#...@...#
###W#S#F#
```

## `forced` -- item hand-off over counters

13x9, two sealed halves: the open tiles form two components with no path between them. Left has
veg, meat, the chopping board, plates and the sink; right has the pot, the fryer and the pass.
Six divider cells are counters with an open tile on both sides -- the **only** way an item
crosses. The order board sits *in* the divider, adjacent to both halves, so neither side is blind
to the tickets.

```
##V#M########
#.....#.....#
#..@..#..@..#
#X....#....O#
#.....B.....#
#L....#....F#
#..@..#..@..#
#.....#.....#
####W####S###
```

## `crowded` -- traffic through a choke point

11x7, the same split as `forced` except the divider has exactly one gap. Prep is left, cooking
and the pass are right, so every ingredient and every plate goes through that one tile.

```
##V#M##B###
#.@..#..@.#
#X...#...O#
#.........#
#L...#...F#
#.@..#..@.#
##W####S###
```

## `asymmetric` -- task allocation under unequal access

15x9, a 3-wide central block with aisles only along the top and bottom rows. The right half owns
the pot, the fryer **and** the pass -- it can cook and serve without walking; the left half owns
everything else. The halves are connected, so this is not `forced`; it is unequal, which is the
point.

```
###V#M#####B###
#.............#
#..@..###..@..#
#X....###....O#
#.....###....F#
#L....###....S#
#..@..###..@..#
#.............#
#####W#########
```

## `circuit` -- when to hand off instead of walk

15x7, a 7-cell counter island down the middle of a loop: walking round it is twelve steps,
putting the item on it and letting the other side take it is two.

```
###V#M###B#####
#.............#
#..@.......@..#
#X..#######..O#
#..@.......@..#
#.............#
####L###W#S#F##
```

## `ring` -- traffic discipline

11x9, a solid 7x5 block with a one-tile-wide corridor all the way round. Two cogs meeting
head-on cannot pass; one must back into a corner. This is what the plan field `yield_to` exists
for.

```
###V#M#B###
#..@...@..#
#.#######.#
X.#######.O
#.#######.#
L.#######.F
#.#######.#
#..@...@..#
###W#S#####
```

## `figure-eight` -- right of way on a shared spine

15x9, two one-tile loops sharing the central column. Everything crossing between loops fights for
the same spine.

```
####V#M#B######
#.@.........@.#
#.#####.#####.#
X.#####.#####.O
#.#####.#####.#
L.#####.#####.F
#.#####.#####.#
#.@.........@.#
####W###S######
```
