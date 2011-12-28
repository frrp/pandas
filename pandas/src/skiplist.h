
/*
  Flexibly-sized, indexable skiplist data structure for maintaining a sorted
  list of values

  Port of Wes McKinney's Cython version of Raymond Hettinger's original pure
  Python recipe (http://rhettinger.wordpress.com/2010/02/06/lost-knowledge/)
 */

#include <stdio.h>
#include <stdlib.h>
#include <numpy/arrayobject.h>
#include <numpy/npy_math.h>

static inline double Log2(double val) {
  return log(val) / log(2.);
}

typedef struct node_t node_t;

struct node_t {
  double value;
  int is_nil;
  int levels;
  node_t **next;
  int *width;
  int ref_count;
};

typedef struct {
  node_t *head;
  int size, maxlevels;
  node_t **tmp_chain;
  int *tmp_steps;
} skiplist_t;

static inline double urand() {
  return rand() / ((double) RAND_MAX + 1);
}

static inline int int_min(int a, int b) {
  return a < b ? a : b;
}

static inline node_t *node_init(double value, int levels) {
  node_t *result;
  result = (node_t*) calloc(1, sizeof(node_t));

  result->value = value;
  result->levels = levels;
  result->is_nil = 0;
  result->ref_count = 0;

  result->next = (node_t**) malloc(levels * sizeof(node_t*));
  result->width = (int*) malloc(levels * sizeof(int));

  return result;
}

// do this ourselves

static inline void node_incref(node_t *node) {
  node->ref_count += 1;
}

static inline void node_decref(node_t *node) {
  node->ref_count -= 1;
}

static void node_destroy(node_t *node) {
  int i;
  if (node) {
    if (node->ref_count == 1) {
      for (i = 0; i < node->levels; ++i) {
        node_destroy(node->next[i]);
      }
      free(node->next);
      free(node->width);
      // printf("Reference count was 1, freeing\n");
    }
    else {
      node_decref(node);
    }
    // pretty sure that freeing the struct above will be enough
    // free(node);
  }
}

static inline skiplist_t *skiplist_init(int expected_size) {
  skiplist_t *result;
  node_t *NIL, *head;
  int maxlevels, i;

  maxlevels = Log2((double) expected_size);
  result = (skiplist_t*) calloc(1, sizeof(skiplist_t));
  result->tmp_chain = (node_t**) malloc(maxlevels * sizeof(node_t*));
  result->tmp_steps = (int*) malloc(maxlevels * sizeof(int));
  result->maxlevels = maxlevels;

  head = result->head = node_init(NPY_NAN, maxlevels);
  node_incref(head);

  NIL = node_init(0, 0);
  NIL->is_nil = 1;

  for (i = 0; i < maxlevels; ++i)
  {
    head->next[i] = NIL;
    head->width[i] = 1;
    node_incref(NIL);
  }

  return result;
}

static inline void skiplist_destroy(skiplist_t *skp) {
  if (skp) {
    node_destroy(skp->head);
    free(skp->tmp_steps);
    free(skp->tmp_chain);
    free(skp);
  }
}


// 1 if left < right, 0 if left == right, -1 if left > right

static inline int _node_cmp(node_t* node, double value){
  if (node->is_nil || node->value > value) {
    return -1;
  }
  else if (node->value < value) {
    return 1;
  }
  else {
    return 0;
  }
}

static inline double skiplist_get(skiplist_t *skp, int i, int *ret) {
  node_t *node;
  int level;

  if (i < 0 || i >= skp->size) {
    *ret = 0;
    return 0;
  }

  node = skp->head;
  i++;
  for (level = skp->maxlevels - 1; level >= 0; --level)
  {
    while (node->width[level] <= i)
    {
      i = i - node->width[level];
      node = node->next[level];
    }
  }

  *ret = 1;
  return node->value;
}

static inline int skiplist_insert(skiplist_t *skp, double value) {
  node_t *node, *prevnode, *newnode, *next_at_level, *tmp;
  int *steps_at_level;
  int size, steps, level;
  node_t **chain;

  chain = skp->tmp_chain;

  steps_at_level = skp->tmp_steps;
  memset(steps_at_level, 0, skp->maxlevels * sizeof(int));

  node = skp->head;

  for (level = skp->maxlevels - 1; level >= 0; --level)
  {
    next_at_level = node->next[level];
    while (_node_cmp(next_at_level, value) >= 0) {
      steps_at_level[level] += node->width[level];
      node = next_at_level;
      next_at_level = node->next[level];
    }
    chain[level] = node;
  }

  size = int_min(skp->maxlevels, 1 - ((int) Log2(urand())));

  newnode = node_init(value, size);
  steps = 0;

  for (level = 0; level < size; ++level) {
    prevnode = chain[level];
    newnode->next[level] = prevnode->next[level];

    prevnode->next[level] = newnode;
    node_incref(newnode); // increment the reference count

    newnode->width[level] = prevnode->width[level] - steps;
    prevnode->width[level] = steps + 1;

    steps += steps_at_level[level];
  }

  for (level = size; level < skp->maxlevels; ++level) {
    chain[level]->width[level] += 1;
  }

  skp->size++;

  return 1;
}

static inline int skiplist_remove(skiplist_t *skp, double value) {
  int level, size;
  node_t *node, *prevnode, *tmpnode, *next_at_level;
  node_t **chain;

  chain = skp->tmp_chain;
  node = skp->head;

  for (level = skp->maxlevels - 1; level >= 0; --level)
  {
    next_at_level = node->next[level];
    while (_node_cmp(next_at_level, value) > 0) {
      node = next_at_level;
      next_at_level = node->next[level];
    }
    chain[level] = node;
  }

  if (value != chain[0]->next[0]->value) {
    return 0;
  }

  size = chain[0]->next[0]->levels;

  for (level = 0; level < size; ++level) {
    prevnode = chain[level];

    tmpnode = prevnode->next[level];

    prevnode->width[level] += tmpnode->width[level] - 1;
    prevnode->next[level] = tmpnode->next[level];

    tmpnode->next[level] = NULL;
    node_destroy(tmpnode); // decrement refcount or free
  }

  for (level = size; level < skp->maxlevels; ++level) {
    chain[level]->width[level] -= 1;
  }

  skp->size--;
  return 1;
}